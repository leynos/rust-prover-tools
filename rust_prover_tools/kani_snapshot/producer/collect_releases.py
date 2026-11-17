"""Collect this repository's existing releases for identity allocation.

Allocating a publication revision is a decision about tags that already
exist, so the resolve job must observe them. This module projects
`gh release list` into the one document `identity --releases` parses: a JSON
array of `{tag, builder_commit}` records.

The list is bounded, and reaching the bound is a failure rather than a
silently truncated view. A truncated view could hide the newest revision and
make the allocator reuse it, which is the one mistake an immutable identity
cannot recover from.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.lifecycle import write_document
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_mapping,
    require_sequence,
    require_text,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("collect_releases", "main")

#: Releases read per run. Reaching it is an actionable failure, not a ceiling
#: that quietly hides the newest tag.
RELEASE_LIMIT: typ.Final[int] = 100


def _gh_json(arguments: cabc.Sequence[str], what: str) -> object:
    """Run one `gh` read and parse its output strictly."""
    result = run_command(CommandSpec(("gh", *arguments)))
    if result.exit_code != 0:
        msg = f"gh failed for {what}: {result.stderr.strip()}"
        raise ProverToolError(msg)
    return strict_json_loads(result.stdout.encode("utf-8"), what)


def collect_releases() -> bytes:
    """Collect the release list identity allocation consumes.

    Returns
    -------
    bytes
        Deterministic JSON array of `{tag, builder_commit}` objects in the
        order `gh` returned them.

    Raises
    ------
    ProverToolError
        Raised when `gh` fails, a record is not the expected shape, or the
        bounded list was exhausted.
    """
    listed = require_sequence(
        _gh_json(
            [
                "release",
                "list",
                "--limit",
                str(RELEASE_LIMIT),
                "--json",
                "tagName,targetCommitish",
            ],
            "release list",
        ),
        "release list",
    )
    if len(listed) >= RELEASE_LIMIT:
        msg = (
            f"release list reached its {RELEASE_LIMIT}-release bound; raise the "
            "bound deliberately after reviewing the oldest releases"
        )
        raise ProverToolError(msg)
    releases: list[dict[str, str]] = []
    for index, item in enumerate(listed):
        record = require_mapping(item, f"release {index}")
        releases.append({
            "tag": require_truthy_text(record.get("tagName"), f"release {index} tag"),
            "builder_commit": require_text(
                record.get("targetCommitish"), f"release {index} targetCommitish"
            ),
        })
    text = json.dumps(releases, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def build_parser() -> argparse.ArgumentParser:
    """Return the release collector's command line parser."""
    parser = argparse.ArgumentParser(
        prog="kani-snapshot-releases",
        description="Collect existing releases for identity allocation.",
    )
    parser.add_argument("--output", required=True)
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write the release list for one resolve job."""
    namespace = build_parser().parse_args(argv)
    try:
        write_document(pathlib.Path(namespace.output), collect_releases())
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-releases: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
