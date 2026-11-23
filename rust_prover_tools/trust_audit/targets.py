"""Discover the Cargo targets one crate directory contributes to the audit.

Cargo compiles more files as crate roots than a directory listing suggests:
``src/lib.rs``, ``src/main.rs``, ``src/bin/*.rs`` and ``src/bin/*/main.rs``,
plus the top-level entries of ``tests``, ``benches`` and ``examples`` and any
path a manifest names explicitly.  Files deeper in those directories are
modules of a target, not targets themselves, so treating every ``.rs`` file as
a driver would resolve its ``mod`` declarations from a directory rustc never
uses.  This module reads the directory layout and the manifest, which is why it
sits at the audit's I/O edge.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.manifests import read_cargo_manifest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

__all__ = ("entry_points",)

#: A Rust source file's suffix.
_RUST_SUFFIX: typ.Final[str] = ".rs"
#: The module roots a manifestless directory may contribute.
_CRATE_ROOTS: typ.Final[tuple[str, ...]] = ("lib.rs", "main.rs", "mod.rs")
#: Directories whose top-level entries are crate roots in their own right.
_COMPILED_DIRECTORIES: typ.Final[tuple[str, ...]] = ("tests", "benches", "examples")
#: The manifest that declares targets beyond the directory layout.
_CARGO_MANIFEST: typ.Final[str] = "Cargo.toml"
#: The library path Cargo auto-discovers.
_DEFAULT_LIBRARY: typ.Final[str] = "src/lib.rs"

type _TargetKind = typ.Literal["lib", "bin", "test", "bench", "example"]


def entry_points(
    root: pathlib.Path,
    crate_root: pathlib.Path,
) -> cabc.Iterator[pathlib.Path]:
    """Yield every file Cargo compiles as a crate root in ``crate_root``.

    A directory with no manifest contributes the conventional module roots and
    target-shaped entries, because it is not a Cargo package and has no
    manifest whose flags could narrow the answer.  A directory with a manifest
    follows Cargo's target rules: the library and binary layout, the top-level
    entries of the test, bench and example directories, and every target the
    manifest names explicitly.  A virtual workspace manifest declares no
    package and therefore no targets of its own.
    """
    manifest = crate_root / _CARGO_MANIFEST
    if not manifest.is_file():
        yield from _conventional_roots(crate_root)
        return
    document = _read_manifest(root, manifest)
    if not _declares_package(document):
        return
    yield from _auto_targets(crate_root, document)
    yield from _manifest_targets(crate_root, manifest, document)


def _declares_package(document: dict[str, object]) -> bool:
    """Report whether a manifest declares a package rather than only a workspace."""
    return isinstance(document.get("package"), dict) or isinstance(
        document.get("project"),
        dict,
    )


def _conventional_roots(crate_root: pathlib.Path) -> cabc.Iterator[pathlib.Path]:
    """Yield the target-shaped entries of one manifestless directory."""
    for name in _CRATE_ROOTS:
        for candidate in (crate_root / name, crate_root / "src" / name):
            if candidate.is_file():
                yield candidate
    yield from _directory_targets(crate_root / "src" / "bin")
    for directory in _COMPILED_DIRECTORIES:
        yield from _directory_targets(crate_root / directory)


def _auto_targets(
    crate_root: pathlib.Path,
    document: dict[str, object],
) -> cabc.Iterator[pathlib.Path]:
    """Yield the targets Cargo auto-discovers under one manifest."""
    flags = _auto_discovery(document)
    if flags["lib"] and not _explicit_library_path(document):
        candidate = crate_root / _DEFAULT_LIBRARY
        if candidate.is_file():
            yield candidate
    if flags["bin"]:
        main = crate_root / "src" / "main.rs"
        if main.is_file():
            yield main
        yield from _directory_targets(crate_root / "src" / "bin")
    if flags["example"]:
        yield from _directory_targets(crate_root / "examples")
    if flags["test"]:
        yield from _directory_targets(crate_root / "tests")
    if flags["bench"]:
        yield from _directory_targets(crate_root / "benches")


def _directory_targets(directory: pathlib.Path) -> cabc.Iterator[pathlib.Path]:
    """Yield the ``*.rs`` files and ``*/main.rs`` entries of one directory.

    This is Cargo's ``infer_from_directory``: dotfiles are skipped, a file is a
    target of its own, and a directory is one only through its ``main.rs``.
    Every deeper file is a module, which the audit's module walk reaches when
    its declaration is followed.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix == _RUST_SUFFIX:
            yield entry
        elif entry.is_dir() and (entry / "main.rs").is_file():
            yield entry / "main.rs"


def _read_manifest(
    root: pathlib.Path,
    manifest: pathlib.Path,
) -> dict[str, object]:
    """Read one manifest, checking first that its resolved file is in root."""
    resolved = manifest.resolve()
    if resolved != root and root not in resolved.parents:
        msg = (
            f"{manifest} resolves outside the repository root {root}, so its "
            "targets cannot be audited"
        )
        raise TrustAuditError(msg)
    return read_cargo_manifest(resolved, unknown="its targets are unknown")


def _explicit_library_path(document: dict[str, object]) -> bool:
    """Report whether the manifest names the library's path explicitly."""
    library = document.get("lib")
    return isinstance(library, dict) and library.get("path") is not None


def _auto_discovery(document: dict[str, object]) -> dict[str, bool]:
    """Return whether Cargo auto-discovers each target kind.

    The five ``auto*`` keys disable one kind each.  A non-boolean value is
    refused rather than guessed, as is Cargo's 2015-edition default: an
    explicitly declared target of a kind disables auto-discovery for that kind
    unless the manifest sets the flag.
    """
    package = document.get("package")
    settings = package if isinstance(package, dict) else {}
    explicit = _declared_kinds(document)
    legacy = _edition(package) == "2015"
    flags: dict[str, bool] = {}
    for flag, kind in (
        ("autolib", "lib"),
        ("autobins", "bin"),
        ("autoexamples", "example"),
        ("autotests", "test"),
        ("autobenches", "bench"),
    ):
        value = settings.get(flag)
        if value is None:
            flags[kind] = not (legacy and kind in explicit)
        elif isinstance(value, bool):
            flags[kind] = value
        else:
            msg = f"the manifest sets package.{flag} to a non-boolean value"
            raise TrustAuditError(msg)
    return flags


def _edition(package: object) -> str:
    """Return the package's edition, defaulting to Cargo's 2015 default."""
    if isinstance(package, dict):
        edition = package.get("edition")
        if isinstance(edition, str):
            return edition
    return "2015"


def _declared_kinds(document: dict[str, object]) -> frozenset[str]:
    """Return the target kinds the manifest declares a table for."""
    kinds = set()
    if isinstance(document.get("lib"), dict):
        kinds.add("lib")
    for key, kind in (
        ("bin", "bin"),
        ("test", "test"),
        ("bench", "bench"),
        ("example", "example"),
    ):
        if isinstance(document.get(key), list):
            kinds.add(kind)
    return frozenset(kinds)


def _manifest_targets(
    crate_root: pathlib.Path,
    manifest: pathlib.Path,
    document: dict[str, object],
) -> cabc.Iterator[pathlib.Path]:
    """Yield every target the manifest names, refusing one that resolves to none.

    A declared ``path`` is read as Cargo reads it, relative to the manifest's
    directory.  A target with no path is inferred from its name by Cargo's own
    layout rules.  Either way the file must exist: silently skipping a target
    Cargo would try to build would leave part of the crate unaudited while the
    audit exited successfully.
    """
    package_name = _package_name(document)
    for kind, table in _target_tables(document):
        declared = table.get("path")
        name = table.get("name")
        if declared is not None:
            if not isinstance(declared, str):
                msg = f"{manifest} declares a {kind} target path that is not a string"
                raise TrustAuditError(msg)
            candidate = crate_root / declared
            if not candidate.is_file():
                msg = (
                    f"{manifest} names the {kind} target {name!r} at "
                    f"{declared!r}, which does not exist"
                )
                raise TrustAuditError(msg)
            yield candidate
            continue
        candidates = _named_candidates(crate_root, kind, name, package_name)
        if not candidates:
            msg = (
                f"{manifest} declares the {kind} target {name!r}, but no file "
                "in Cargo's layout supplies it"
            )
            raise TrustAuditError(msg)
        if len(candidates) > 1:
            msg = (
                f"{manifest} declares the {kind} target {name!r}, which "
                f"Cargo cannot infer because several files match: {candidates}"
            )
            raise TrustAuditError(msg)
        yield candidates[0]


def _named_candidates(
    crate_root: pathlib.Path,
    kind: _TargetKind,
    name: object,
    package_name: str | None,
) -> list[pathlib.Path]:
    """Return the files Cargo would infer for one named target."""
    if kind == "lib":
        candidate = crate_root / _DEFAULT_LIBRARY
        return [candidate] if candidate.is_file() else []
    if not isinstance(name, str) or not name:
        return []
    if kind == "bin":
        candidates = []
        if package_name is not None and name == package_name:
            candidates.append(crate_root / "src" / "main.rs")
        candidates.extend(
            (
                crate_root / "src" / "bin" / f"{name}{_RUST_SUFFIX}",
                crate_root / "src" / "bin" / name / "main.rs",
            ),
        )
    else:
        directory = crate_root / f"{kind}s"
        candidates = [
            directory / f"{name}{_RUST_SUFFIX}",
            directory / name / "main.rs",
        ]
    return [candidate for candidate in candidates if candidate.is_file()]


def _package_name(document: dict[str, object]) -> str | None:
    """Return the package's name, which names the default binary."""
    package = document.get("package")
    if not isinstance(package, dict):
        return None
    name = package.get("name")
    return name if isinstance(name, str) else None


def _target_tables(
    document: dict[str, object],
) -> cabc.Iterator[tuple[_TargetKind, dict[str, object]]]:
    """Yield every target table a manifest may declare a path in."""
    library = document.get("lib")
    if isinstance(library, dict):
        yield "lib", library
    for key, kind in (
        ("bin", "bin"),
        ("test", "test"),
        ("bench", "bench"),
        ("example", "example"),
    ):
        entries = document.get(key)
        if not isinstance(entries, list):
            continue
        for table in entries:
            if isinstance(table, dict):
                yield typ.cast("_TargetKind", kind), table
