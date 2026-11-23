"""Adapt the trust audit and a repository root to the command line.

The adapter maps arguments, asks :mod:`rust_prover_tools.trust_audit` for the
drivers and the audit, and renders the report.  Every file the audit reads must
sit beneath the repository root, because a reported location is a
repository-relative path; that boundary and the driver expansion belong to the
package rather than to the command.

The status says whether the audit *completed*, not whether it liked the answer.
A proof may legitimately ask its verifier to accept something, and this command
has no record of which requests were justified, so a non-empty report is not a
failure. Only source that could not be audited is.
"""

from __future__ import annotations

import json
import sys
import typing as typ
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.trust_audit import AuditedFile, TrustAuditError, audit_tree
from rust_prover_tools.trust_audit.drivers import expand_drivers
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
        drivers = expand_drivers(root, paths)
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
