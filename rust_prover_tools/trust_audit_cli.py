"""Adapt the trust audit and a repository root to the command line.

The audit reads source files, and every file it reads must sit beneath the
repository root: a location it reports is a repository-relative path, so a
target outside the root would name a file the report cannot describe. The
adapter owns that boundary, the output shape and the exit status; deciding what
is trust belongs to :mod:`rust_prover_tools.trust_audit`.

The status says whether the audit *completed*, not whether it liked the answer.
A proof may legitimately ask its verifier to accept something, and this command
has no record of which requests were justified, so a non-empty report is not a
failure. Only source that could not be audited is.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tomllib
import typing as typ
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.trust_audit import AuditedFile, TrustAuditError, audit_tree
from rust_prover_tools.trust_audit.module_paths import RUST_SUFFIX
from rust_prover_tools.trust_audit.vocabulary import (
    Vocabulary,
    select_vocabularies,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.trust_audit.model import TrustItem

__all__ = ("REPORT_SCHEMA_VERSION", "audit_trust")

#: Schema version of the JSON object this command writes.
REPORT_SCHEMA_VERSION: typ.Final[int] = 1
#: Exit status of an audit that could not complete.
AUDIT_FAILED: typ.Final[int] = 1


def _entries(
    files: cabc.Iterable[AuditedFile],
) -> tuple[tuple[str, TrustItem], ...]:
    """Pair every trusted item with the file it was found in."""
    return tuple((file.path, item) for file in files for item in file.items)


def _provers(vocabularies: tuple[Vocabulary, ...]) -> tuple[str, ...]:
    """Name the selected provers in report order."""
    return tuple(vocabulary.prover for vocabulary in vocabularies)


def _text_lines(
    files: tuple[AuditedFile, ...],
    vocabularies: tuple[Vocabulary, ...],
) -> tuple[str, ...]:
    """Render the human-readable form: the selection, items, then a count."""
    entries = _entries(files)
    count = len(entries)
    noun = "item" if count == 1 else "items"
    audited = len(files)
    file_noun = "file" if audited == 1 else "files"
    return (
        f"provers: {', '.join(_provers(vocabularies))}",
        *(
            f"{_printable(path)}:{item.line}:{item.column}: {item.item}"
            for path, item in entries
        ),
        f"audited {audited} {file_noun}, {count} trusted {noun}",
    )


def _printable(path: str) -> str:
    """Escape characters that would break one line of the text report.

    A Linux filename may contain a newline or an escape character, and the
    report must keep one item per line.  The JSON form already escapes them.
    """
    if path.isprintable():
        return path
    return path.encode("unicode_escape").decode("ascii")


def _json_payload(
    files: tuple[AuditedFile, ...],
    vocabularies: tuple[Vocabulary, ...],
) -> dict[str, object]:
    """Render the stable machine-readable form."""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "provers": list(_provers(vocabularies)),
        "files_audited": len(files),
        "items": [
            {
                "item": item.item,
                "path": path,
                "line": item.line,
                "column": item.column,
            }
            for path, item in _entries(files)
        ],
    }


# The public command needs one parameter per documented flag.
# pylint: disable-next=too-many-arguments
def audit_trust(  # ruff: ignore[too-many-arguments] - one keyword per CLI flag
    paths: typ.Annotated[list[Path], Parameter(help="Rust files to start from")],
    *,
    repo_root: typ.Annotated[
        Path | None,
        Parameter(name="--repo-root", alias="--root", env_var="INPUT_REPO_ROOT"),
    ] = None,
    procedural_macro: typ.Annotated[
        bool | None,
        Parameter(
            name="--procedural-macro",
            negative="--no-procedural-macro",
            env_var="INPUT_PROCEDURAL_MACRO",
        ),
    ] = None,
    prover: typ.Annotated[
        list[str] | None,
        Parameter(name="--prover", allow_repeating=True, negative=()),
    ] = None,
    output_format: typ.Annotated[
        typ.Literal["text", "json"],
        Parameter(name="--format"),
    ] = "text",
) -> int:
    """Audit Rust sources for items the verifier was told to accept.

    Parameters
    ----------
    paths : list[Path]
        Files to start from, usually each crate's ``lib.rs`` or ``main.rs``. A
        relative path is read as repository-relative; an absolute one must
        already sit beneath ``repo_root``. Every module they declare is audited
        too, and each must sit beneath the root as well.
    repo_root : Path | None
        Repository root that bounds the audit and anchors every reported
        location. Defaults to the current working directory.
    procedural_macro : bool | None
        Scan string literals as well as code, for a proc-macro crate that can
        turn them into code.  By default the crate kind is read from the nearest
        `Cargo.toml` above each source; either flag states it for every source
        instead.
    prover : list[str] | None
        Provers whose vocabularies to apply, named `kani` or `verus`. This
        option may be repeated; without it both vocabularies are applied.
    output_format : Literal["text", "json"]
        Output form, defaulting to text.

    Returns
    -------
    int
        ``0`` when every source was audited, and ``1`` when the audit could not
        complete because a source was missing, unreadable or outside the root,
        or a prover name is unknown.

    Notes
    -----
    A refused source is a failure of the audit, not a finding: the report would
    otherwise describe a proof whose code was never read.
    """
    try:
        vocabularies = select_vocabularies(prover)
    except ValueError as error:
        print_lines((str(error),), stream=sys.stderr)
        return AUDIT_FAILED
    root = (repo_root or Path.cwd()).resolve()
    try:
        drivers = _expand_drivers(root, paths)
        files = audit_tree(
            root,
            drivers,
            procedural_macro=procedural_macro,
            vocabularies=vocabularies,
        )
    except TrustAuditError as error:
        print_lines((str(error),), stream=sys.stderr)
        return AUDIT_FAILED
    if output_format == "json":
        lines: tuple[str, ...] = (json.dumps(_json_payload(files, vocabularies)),)
    else:
        lines = _text_lines(files, vocabularies)
    print_lines(lines)
    return 0


#: The module roots a crate directory contributes.
_CRATE_ROOTS: typ.Final[tuple[str, ...]] = ("lib.rs", "main.rs", "mod.rs")
#: Directories whose Rust files are crate roots in their own right.
_COMPILED_DIRECTORIES: typ.Final[tuple[str, ...]] = ("tests", "benches", "examples")


def _expand_drivers(root: Path, paths: cabc.Iterable[Path]) -> tuple[str, ...]:
    """Expand files and crate directories into repository-relative drivers.

    A file is its own driver.  A directory contributes the entry points of
    every crate beneath it: the module roots at the crate directory and under
    ``src/``, and every Rust file under ``tests/``, ``benches/`` and
    ``examples/``.  No entry point is a refusal, because an audit that read
    nothing must not look like a clean one.
    """
    drivers: dict[str, None] = {}
    for path in paths:
        for driver in _drivers_for(root, path):
            drivers[driver] = None
    return tuple(drivers)


def _drivers_for(root: Path, path: Path) -> tuple[str, ...]:
    """Return the drivers one argument names, refusing a missing path."""
    candidate = path if path.is_absolute() else root / path
    if not candidate.is_dir():
        # A file, a missing path, or one outside the root keeps the ordinary
        # single-driver path, so its existing diagnostics are unchanged.
        return (_relative_to(root, path),)
    resolved = candidate.resolve()
    _require_inside(root, resolved, path)
    drivers = {
        _contained_relative(root, entry.resolve(), path).as_posix()
        for crate_root in _crate_roots(root, resolved, path)
        for entry in _entry_points(crate_root)
    }
    if not drivers:
        msg = f"{path} contains no Rust entry point to audit"
        raise TrustAuditError(msg)
    return tuple(sorted(drivers))


def _require_inside(root: Path, resolved: Path, path: Path) -> None:
    """Refuse a path whose resolved form leaves the repository root."""
    if resolved != root and root not in resolved.parents:
        msg = f"{path} resolves outside the repository root {root}"
        raise TrustAuditError(msg)


def _contained_relative(root: Path, resolved: Path, path: Path) -> Path:
    """Return a resolved entry's repository-relative path, refusing escape."""
    _require_inside(root, resolved, path)
    return resolved.relative_to(root)


def _crate_roots(
    root: Path,
    directory: Path,
    path: Path,
) -> tuple[Path, ...]:
    """Return every crate directory under ``directory``, including itself."""
    manifests = set()
    for manifest in directory.rglob("Cargo.toml"):
        resolved = manifest.parent.resolve()
        _contained_relative(root, resolved, path)
        manifests.add(resolved)
    return tuple(sorted(manifests | {directory}))


def _entry_points(crate_root: Path) -> cabc.Iterator[Path]:
    """Yield the files that start a crate's module tree.

    Cargo compiles more entry points than the conventional module roots: every
    file under ``src/bin`` is a binary crate, and a manifest may name an
    explicit ``path`` for a library, binary, test, bench or example.  A reader
    that missed one could report a clean audit that never read it.
    """
    for name in _CRATE_ROOTS:
        for candidate in (crate_root / name, crate_root / "src" / name):
            if candidate.is_file():
                yield candidate
    binary_directory = crate_root / "src" / "bin"
    if binary_directory.is_dir():
        yield from sorted(binary_directory.rglob(f"*{RUST_SUFFIX}"))
    yield from _manifest_targets(crate_root)
    for directory in _COMPILED_DIRECTORIES:
        nested = crate_root / directory
        if nested.is_dir():
            yield from sorted(nested.rglob(f"*{RUST_SUFFIX}"))


def _manifest_targets(crate_root: Path) -> cabc.Iterator[Path]:
    """Yield the target paths a crate's ``Cargo.toml`` names explicitly."""
    manifest = crate_root / "Cargo.toml"
    if not manifest.is_file():
        return
    document = _read_target_manifest(manifest)
    for table in _target_tables(document):
        value = _target_path(manifest, table)
        if value is None:
            continue
        candidate = crate_root / value
        if candidate.is_file():
            yield candidate


def _read_target_manifest(manifest: Path) -> dict[str, object]:
    """Return a manifest whose target paths are about to be read."""
    try:
        return tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"{manifest} cannot be read, so its targets are unknown: {error}"
        raise TrustAuditError(msg) from error


def _target_tables(document: dict[str, object]) -> list[dict[str, object]]:
    """Return every target table a manifest may declare a path in."""
    tables: list[object] = [document.get("lib")]
    for key in ("bin", "test", "bench", "example"):
        entries = document.get(key)
        if isinstance(entries, list):
            tables.extend(entries)
    return [table for table in tables if isinstance(table, dict)]


def _target_path(manifest: Path, table: dict[str, object]) -> str | None:
    """Return one target's explicit path, refusing a non-string value."""
    value = table.get("path")
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{manifest} declares a target path that is not a string"
        raise TrustAuditError(msg)
    return value


def _relative_to(root: Path, path: Path) -> str:
    """Return ``path`` as a repository-relative POSIX path.

    A relative path is read as repository-relative rather than as relative to
    the working directory, so the answer does not depend on where the command
    was started: ``--repo-root R src/lib.rs`` names ``R/src/lib.rs`` from any
    directory. Containment is still checked downstream, so a relative path that
    climbs out of the root is refused there.

    Parameters
    ----------
    root : Path
        Resolved repository root.
    path : Path
        File to start the audit from.

    Returns
    -------
    str
        The path relative to ``root``, as POSIX text.

    Raises
    ------
    TrustAuditError
        If an absolute ``path`` resolves outside ``root``, or reaches an
        in-root file only through a symbolic link outside it.
    """
    if not path.is_absolute():
        return pathlib.PurePosixPath(path.as_posix()).as_posix()
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        msg = f"{path} is outside the repository root {root}"
        raise TrustAuditError(msg) from error
    try:
        # Keep the spelling the caller supplied; the module walk resolves its
        # children from that spelling's directory, exactly as rustc does.
        return path.relative_to(root).as_posix()
    except ValueError as error:
        # The path names an in-root file only through a link outside the root.
        # Substituting the resolved spelling would resolve children from the
        # target's directory while the compiler uses the supplied one.
        msg = (
            f"{path} names a file inside {root} only through a symbolic link, "
            "so its module directory cannot be derived from the spelling"
        )
        raise TrustAuditError(msg) from error
