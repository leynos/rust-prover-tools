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
import typing as typ
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.trust_audit import AuditedFile, TrustAuditError, audit_tree

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


def _text_lines(files: tuple[AuditedFile, ...]) -> tuple[str, ...]:
    """Render the human-readable form: one item per line, then a count."""
    entries = _entries(files)
    count = len(entries)
    noun = "item" if count == 1 else "items"
    return (
        *(f"{path}:{item.line}:{item.column}: {item.item}" for path, item in entries),
        f"audited {len(files)} files, {count} trusted {noun}",
    )


def _json_payload(files: tuple[AuditedFile, ...]) -> dict[str, object]:
    """Render the stable machine-readable form."""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
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


def audit_trust(
    paths: typ.Annotated[list[Path], Parameter(help="Rust files to start from")],
    *,
    repo_root: typ.Annotated[
        Path | None,
        Parameter(name="--repo-root", env_var="INPUT_REPO_ROOT"),
    ] = None,
    procedural_macro: typ.Annotated[
        bool,
        Parameter(
            name="--procedural-macro",
            negative="--no-procedural-macro",
            env_var="INPUT_PROCEDURAL_MACRO",
        ),
    ] = False,
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
    procedural_macro : bool
        Scan string literals as well as code, for a proc-macro crate that can
        turn them into code.
    output_format : Literal["text", "json"]
        Output form, defaulting to text.

    Returns
    -------
    int
        ``0`` when every source was audited, and ``1`` when the audit could not
        complete because a source was missing, unreadable or outside the root.

    Notes
    -----
    A refused source is a failure of the audit, not a finding: the report would
    otherwise describe a proof whose code was never read.
    """
    root = (repo_root or Path.cwd()).resolve()
    try:
        drivers = tuple(_relative_to(root, path) for path in paths)
        files = audit_tree(root, drivers, procedural_macro=procedural_macro)
    except TrustAuditError as error:
        print_lines((str(error),), stream=sys.stderr)
        return AUDIT_FAILED
    if output_format == "json":
        lines: tuple[str, ...] = (json.dumps(_json_payload(files)),)
    else:
        lines = _text_lines(files)
    print_lines(lines)
    return 0


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
        If an absolute ``path`` resolves outside ``root``, where the audit
        could not name it.
    """
    if not path.is_absolute():
        return pathlib.PurePosixPath(path.as_posix()).as_posix()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        msg = f"{path} is outside the repository root {root}"
        raise TrustAuditError(msg) from error
