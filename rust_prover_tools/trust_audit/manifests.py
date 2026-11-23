"""Read the crate kind a Cargo manifest declares.

The trust audit reads string literals as code only for a proc-macro crate,
because such a crate can turn a literal into an item the lexer never sees.
Cargo records that fact in the manifest, so this module answers it from the
nearest ``Cargo.toml`` rather than guessing from a file's name or path.

This module reads the filesystem, as do `.drivers`, `.targets` and `.tree`.
Target discovery and proc-macro detection share `read_cargo_manifest`, so one
decoder answers both questions.
"""

from __future__ import annotations

import dataclasses as dc
import tomllib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

__all__ = (
    "CARGO_MANIFEST",
    "CrateKind",
    "detect_crate_kind",
    "read_cargo_manifest",
)

#: The manifest that says whether a crate is a proc-macro crate.
CARGO_MANIFEST: str = "Cargo.toml"

#: Cargo packages whose names a manifest may rename to another key.
_TRUST_PACKAGES: frozenset[str] = frozenset({"kani", "vstd", "verus_builtin"})

#: The library path Cargo auto-discovers when a manifest names none.
_DEFAULT_LIBRARY: str = "src/lib.rs"


@dc.dataclass(frozen=True, slots=True)
class CrateKind:
    """The crate kind a manifest declares, as a value rather than a guess."""

    procedural_macro: bool


def detect_crate_kind(
    root: pathlib.Path,
    relative: str,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]] | None = None,
) -> CrateKind:
    """Report whether the crate owning ``relative`` is a proc-macro crate.

    A manifest the audit cannot parse is refused: it says which crate kinds
    exist, and guessing would decide whether literals are scanned as code from
    a file nobody read.  A file with no manifest above it belongs to no crate
    whose kind Cargo can declare, so it is not a proc-macro crate.  A driver
    listed by a manifest's ``[lib] path`` is owned by that manifest even when
    it sits outside the manifest's directory; that declaration is searched for
    repository-wide before the answer falls back to false.

    The repository-wide manifest search is I/O, so it lives here rather than
    in the walker.  A caller that owns the read policy can supply
    ``read_manifest``; the default reads strict UTF-8 TOML.

    Parameters
    ----------
    root : Path
        Repository root, which bounds the search.
    relative : str
        Repository-relative file whose owning crate is asked about.
    read_manifest : Callable[[Path], dict[str, object]]
        Reads and decodes one manifest, raising :class:`TrustAuditError` when
        it cannot.

    Returns
    -------
    CrateKind
        ``procedural_macro`` is true when the owning manifest declares a
        proc-macro library.  A refusal is raised as :class:`TrustAuditError`,
        which is part of this boundary rather than a value the walker can
        mistake for an ordinary crate.

    Raises
    ------
    TrustAuditError
        If the nearest manifest exists but cannot be read or parsed.
    """
    if read_manifest is None:
        read_manifest = _read_manifest
    resolved_root = root.resolve()
    leaf = (resolved_root / relative).resolve()
    # The driver's own spelling decides which crate owns it: its directory is
    # where Cargo looks first for the manifest that names it.  A ``#[path]``
    # that links the driver to a file in another directory also links it to a
    # manifest there, and reading that one would answer for the wrong crate.
    spelling = (resolved_root / relative).parent
    directory = spelling
    while True:
        manifest = directory / CARGO_MANIFEST
        if manifest.is_file():
            resolved = manifest.resolve()
            if resolved_root not in resolved.parents:
                msg = (
                    f"{manifest} resolves outside the repository root "
                    f"{resolved_root}, so its crate kind is unknown"
                )
                raise TrustAuditError(msg)
            _refuse_inherited_trust_rename(
                resolved_root,
                resolved,
                read_manifest=read_manifest,
            )
            if _declares_procedural_macro(resolved, read_manifest=read_manifest):
                return CrateKind(procedural_macro=True)
            return CrateKind(
                _manifest_naming_leaf(
                    resolved_root,
                    leaf,
                    read_manifest=read_manifest,
                )
            )
        if directory == resolved_root or resolved_root not in directory.parents:
            return CrateKind(
                _manifest_naming_leaf(
                    resolved_root,
                    leaf,
                    read_manifest=read_manifest,
                )
            )
        directory = directory.parent


def _manifest_naming_leaf(
    root: pathlib.Path,
    leaf: pathlib.Path,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]],
) -> bool:
    """Report whether any manifest in ``root`` declares ``leaf`` as its library.

    Cargo resolves ``[lib] path`` relative to its manifest, so a driver can sit
    outside that manifest's directory.  Searching upward from the driver then
    finds nothing, and reporting false would leave a proc-macro crate's
    literals unread.  Every manifest in the repository is asked instead.
    """
    for manifest in sorted(root.rglob(CARGO_MANIFEST)):
        resolved = manifest.resolve()
        if root not in resolved.parents:
            continue
        _refuse_inherited_trust_rename(
            root,
            resolved,
            read_manifest=read_manifest,
        )
        if not _declares_procedural_macro(resolved, read_manifest=read_manifest):
            continue
        if _manifest_covers(
            root,
            resolved,
            leaf,
            read_manifest=read_manifest,
        ):
            return True
    return False


def _manifest_covers(
    root: pathlib.Path,
    manifest: pathlib.Path,
    leaf: pathlib.Path,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]],
) -> bool:
    """Report whether ``manifest``'s ``[lib] path`` names ``leaf``."""
    declared = _library_path(manifest, read_manifest=read_manifest)
    if declared is None:
        return False
    target = (manifest.parent / declared).resolve()
    if root not in target.parents and target != root:
        return False
    return target == leaf


def read_cargo_manifest(
    manifest: pathlib.Path,
    *,
    unknown: str,
) -> dict[str, object]:
    """Read and decode one Cargo manifest, refusing an unreadable file.

    Both the crate-kind question and target discovery read ``Cargo.toml``.  One
    decoder keeps them from drifting in how they treat a missing file, a
    non-UTF-8 document or malformed TOML.  ``unknown`` completes the message
    with what the caller cannot answer without the document.
    """
    try:
        return tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"{manifest} cannot be read, so {unknown}: {error}"
        raise TrustAuditError(msg) from error


def _read_manifest(manifest: pathlib.Path) -> dict[str, object]:
    """Return one manifest's parsed document, refusing an unreadable file."""
    return read_cargo_manifest(manifest, unknown="the crate kind is unknown")


def _library_path(
    manifest: pathlib.Path,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]],
) -> str | None:
    """Return a manifest's library path, defaulting to ``src/lib.rs``.

    Cargo defaults a library target to ``src/lib.rs`` when the manifest names
    none.  A proc-macro crate usually declares only ``[lib] proc-macro = true``,
    so returning ``None`` here would leave its default library file owned by no
    manifest, and the audit would scan its literals as ordinary text.
    """
    document = read_manifest(manifest)
    _refuse_renamed_trust_package(manifest, document)
    library = document.get("lib")
    if not isinstance(library, dict):
        return None
    value = library.get("path")
    if value is None:
        return _DEFAULT_LIBRARY
    if not isinstance(value, str):
        msg = f"{manifest} sets [lib] path to a non-string value"
        raise TrustAuditError(msg)
    return value


def _declares_procedural_macro(
    manifest: pathlib.Path,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]],
) -> bool:
    """Report whether one Cargo manifest declares a proc-macro library.

    Cargo accepts two declarations.  ``[lib] proc-macro = true`` is the
    documented flag, and ``crate-type = ["proc-macro"]`` names the same crate
    type through the general field; a manifest using either builds a proc-macro
    crate, so the audit must scan its literals.  The underscore spellings
    ``proc_macro`` and ``crate_type`` are deprecated aliases Cargo still honours
    outside the 2024 edition; reading them is the fail-closed choice, because
    missing the flag leaves literal-generated code unread.  A non-boolean
    ``proc-macro`` value or a non-string crate type is refused rather than
    guessed at.
    """
    document = read_manifest(manifest)
    _refuse_renamed_trust_package(manifest, document)
    library = document.get("lib")
    if not isinstance(library, dict):
        return False
    # Cargo reads ``crate-type`` first: when it is present it decides the crate
    # type, and the ``proc-macro`` flag is only consulted when it is absent.
    crate_types = _manifest_value(library, "crate-type", "crate_type")
    if crate_types is not None:
        return _declares_crate_type(manifest, crate_types)
    flag = _manifest_value(library, "proc-macro", "proc_macro")
    if flag is None:
        return False
    if not isinstance(flag, bool):
        msg = f"{manifest} sets [lib] proc-macro to a non-boolean value"
        raise TrustAuditError(msg)
    return flag


def _refuse_renamed_trust_package(
    manifest: pathlib.Path,
    document: dict[str, object],
) -> None:
    """Refuse a manifest that renames a trust crate as a dependency.

    Cargo lets ``proof = { package = "kani", ... }`` bind the crate under a new
    name without any import, and the per-file scan cannot see that rename.  A
    call through the new name would pass unreported, so the audit refuses the
    manifest rather than a file it cannot read.
    """
    for table in _dependency_tables(document):
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            if not isinstance(spec, dict):
                continue
            package = spec.get("package")
            if package in _TRUST_PACKAGES and _renamed(name, package):
                msg = (
                    f"{manifest} renames the Cargo package {package!r} to "
                    f"{name!r}, so the audit cannot tell which calls reach it"
                )
                raise TrustAuditError(msg)


def _renamed(name: object, package: object) -> bool:
    """Report whether a dependency key differs from its package name.

    ``vstd = { package = "vstd", ... }`` states the package without changing
    the name it binds, so it is the same import as a plain version.  Cargo
    normalizes ``-`` to ``_`` when a package name becomes a crate name, so the
    comparison does the same: ``verus-builtin`` binds ``verus_builtin``.
    """
    if not isinstance(name, str) or not isinstance(package, str):
        return True
    return name.replace("-", "_") != package.replace("-", "_")


def _dependency_tables(document: dict[str, object]) -> list[object]:
    """Return every table a manifest may declare a dependency in."""
    tables: list[object] = [
        document.get("dependencies"),
        document.get("dev-dependencies"),
        document.get("build-dependencies"),
    ]
    workspace = document.get("workspace")
    if isinstance(workspace, dict):
        tables.append(workspace.get("dependencies"))
    target = document.get("target")
    if isinstance(target, dict):
        for platform in target.values():
            if not isinstance(platform, dict):
                continue
            tables.extend(
                (
                    platform.get("dependencies"),
                    platform.get("dev-dependencies"),
                    platform.get("build-dependencies"),
                ),
            )
    return tables


def _refuse_inherited_trust_rename(
    root: pathlib.Path,
    manifest: pathlib.Path,
    *,
    read_manifest: cabc.Callable[[pathlib.Path], dict[str, object]],
) -> None:
    """Refuse a workspace that renames a trust crate for its members.

    A member inherits ``[workspace.dependencies]`` from a manifest above it,
    and its own file may say only ``proof.workspace = true``.  The rename is
    visible only in the workspace root, so that table is checked too.
    """
    directory = manifest.parent
    while directory == root or root in directory.parents:
        candidate = directory / CARGO_MANIFEST
        if candidate.is_file():
            document = read_manifest(candidate.resolve())
            workspace = document.get("workspace")
            if isinstance(workspace, dict):
                _refuse_renamed_trust_package(candidate, document)
                return
        if directory == root:
            return
        directory = directory.parent


def _declares_crate_type(manifest: pathlib.Path, crate_types: object) -> bool:
    """Report whether a ``crate-type`` value names a proc-macro library."""
    if isinstance(crate_types, str):
        return crate_types == "proc-macro"
    if isinstance(crate_types, list):
        if not all(isinstance(item, str) for item in crate_types):
            msg = f"{manifest} sets [lib] crate-type to a non-string entry"
            raise TrustAuditError(msg)
        return "proc-macro" in crate_types
    msg = f"{manifest} sets [lib] crate-type to an unexpected value"
    raise TrustAuditError(msg)


def _manifest_value(
    library: dict[str, object],
    documented: str,
    deprecated: str,
) -> object | None:
    """Return a value written under its documented or deprecated key.

    Cargo's deprecated underscore spellings are still honoured before the 2024
    edition.  A manifest writing both spellings is ambiguous, so it is refused
    rather than read one way.
    """
    first = library.get(documented)
    second = library.get(deprecated)
    if first is not None and second is not None:
        msg = (
            f"the manifest sets both {documented!r} and its deprecated "
            f"spelling {deprecated!r}"
        )
        raise TrustAuditError(msg)
    return first if first is not None else second
