"""Collect this repository's existing releases for identity allocation.

Allocating a publication revision is a decision about tags that already
exist, so the resolve job must observe them — including drafts, which the
release-by-tag endpoint does not report reliably. This module projects the
paginated release API into the one document `identity --releases` parses: a
JSON array of `{tag, builder_commit, draft, body}` records.

The page bound is a failure rather than a silently truncated view. A truncated
view could hide the newest revision and make the allocator reuse it, which is
the one mistake an immutable identity cannot recover from.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import REPOSITORY
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

__all__ = ("PAGE_SIZE", "RELEASE_PAGE_LIMIT", "collect_releases", "main")

#: Releases requested per page.
PAGE_SIZE: typ.Final[int] = 100

#: Pages read per run. Reaching it is an actionable failure, not a ceiling
#: that quietly hides the newest tag.
RELEASE_PAGE_LIMIT: typ.Final[int] = 10

#: Fields the allocator needs from every release.
_RELEASE_QUERY: typ.Final[str] = (
    "[.[] | {tag: .tag_name, builder_commit: .target_commitish, "
    "draft: .draft, body: .body}]"
)


def _gh_json(arguments: cabc.Sequence[str], what: str) -> object:
    """Run one `gh` read and parse its output strictly."""
    result = run_command(CommandSpec(("gh", *arguments)))
    if result.exit_code != 0:
        msg = f"gh failed for {what}: {result.stderr.strip()}"
        raise ProverToolError(msg)
    return strict_json_loads(result.stdout.encode("utf-8"), what)


def _page(number: int) -> list[dict[str, object]]:
    """Read one page of releases in the allocator's shape."""
    items = require_sequence(
        _gh_json(
            [
                "api",
                f"repos/{REPOSITORY}/releases?per_page={PAGE_SIZE}&page={number}",
                "--jq",
                _RELEASE_QUERY,
            ],
            f"release page {number}",
        ),
        f"release page {number}",
    )
    releases: list[dict[str, object]] = []
    for index, item in enumerate(items):
        field = f"release page {number} entry {index}"
        record = require_mapping(item, field)
        draft = record.get("draft")
        body = record.get("body")
        if not isinstance(draft, bool):
            msg = f"{field} draft must be a boolean"
            raise ProverToolError(msg)
        if body is not None and not isinstance(body, str):
            msg = f"{field} body must be a string or null"
            raise ProverToolError(msg)
        releases.append({
            "tag": require_truthy_text(record.get("tag"), f"{field} tag"),
            "builder_commit": require_text(
                record.get("builder_commit"), f"{field} builder_commit"
            ),
            "draft": draft,
            "body": body,
        })
    return releases


def collect_releases() -> bytes:
    """Collect the release list identity allocation consumes.

    Returns
    -------
    bytes
        Deterministic JSON array of `{tag, builder_commit, draft, body}`
        objects, oldest page first.

    Raises
    ------
    ProverToolError
        Raised when `gh` fails, a record is not the expected shape, or the
        bounded list was exhausted.
    """
    releases: list[dict[str, object]] = []
    for number in range(1, RELEASE_PAGE_LIMIT + 1):
        page = _page(number)
        releases.extend(page)
        if len(page) < PAGE_SIZE:
            break
    else:
        msg = (
            f"release list reached its {RELEASE_PAGE_LIMIT}-page bound; raise "
            "the bound deliberately after reviewing the oldest releases"
        )
        raise ProverToolError(msg)
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
