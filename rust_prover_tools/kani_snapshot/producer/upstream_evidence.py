"""Project the upstream API into the evidence resolution reads.

Fetching and shaping are separate concerns. `collect_candidates` performs the
`gh` reads; this module decides what a response may contribute to the evidence
document. Only fields resolution reads are kept, and every field is validated
before it is written, so a hostile or simply unexpected API response cannot
redirect a later request or smuggle a claim into a recorded candidate.

The rendering functions are here too: a document that is deterministic and
validated belongs beside the projection that produced it, not beside the
transport that fetched it.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_mapping,
    require_non_negative_int,
    require_sequence,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "UPSTREAM_TAG_PREFIX",
    "candidates_to_json",
    "checks_to_json",
    "project_check_page",
    "project_check_run",
    "project_release",
)

#: Only a release tagged this way is a Kani release.
UPSTREAM_TAG_PREFIX: typ.Final[str] = "kani-"


def project_release(release: object) -> dict[str, object] | None:
    """Project one release, or return `None` when resolution cannot use it."""
    if not isinstance(release, dict):
        msg = "upstream release list member must be an object"
        raise ProverToolError(msg)
    record = typ.cast("dict[str, object]", release)
    tag = require_truthy_text(record.get("tag_name"), "upstream release tag_name")
    if not tag.startswith(UPSTREAM_TAG_PREFIX):
        return None
    for flag in ("draft", "prerelease"):
        if record.get(flag) is not False:
            return None
    return {
        "tag_name": tag,
        "published_at": require_truthy_text(
            record.get("published_at"), "upstream release published_at"
        ),
        "draft": False,
        "prerelease": False,
    }


def _optional_text(value: object, field: str) -> str | None:
    """Return a string, `None`, or refuse anything else."""
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{field} must be a string or null"
        raise ProverToolError(msg)
    return value


def project_check_run(commit: str, run: object, index: int) -> dict[str, object]:
    """Project one check run, keeping the evidence a reviewer would check.

    The head SHA is required to name the commit that was queried: a response
    about another commit is not evidence about this release. The URLs are
    retained so a recorded candidate can be inspected without re-querying
    GitHub.
    """
    check = require_mapping(run, f"upstream check run {index}")
    head = require_full_sha(
        check.get("head_sha"), f"upstream check run {index} head_sha"
    )
    if head != commit:
        msg = (
            f"upstream check run {index} reports head {head}, not the queried "
            f"commit {commit}"
        )
        raise ProverToolError(msg)
    return {
        "id": check.get("id"),
        "name": check.get("name"),
        "status": check.get("status"),
        "conclusion": check.get("conclusion"),
        "started_at": check.get("started_at"),
        "head_sha": head,
        "html_url": require_truthy_text(
            check.get("html_url"), f"upstream check run {index} html_url"
        ),
        "details_url": _optional_text(
            check.get("details_url"), f"upstream check run {index} details_url"
        ),
    }


def project_check_page(
    document: object,
    commit: str,
) -> tuple[int, list[dict[str, object]]]:
    """Validate one check-run page and project its runs and reported total."""
    record = require_mapping(document, "upstream check-run document")
    total = require_non_negative_int(
        record.get("total_count"), "upstream check-run total_count"
    )
    runs = [
        project_check_run(commit, run, index)
        for index, run in enumerate(
            require_sequence(record.get("check_runs"), "upstream check runs")
        )
    ]
    return total, runs


def candidates_to_json(candidates: cabc.Sequence[dict[str, object]]) -> bytes:
    """Render a candidate document as deterministic JSON bytes."""
    text = json.dumps(list(candidates), sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def checks_to_json(
    commit: str,
    checks: cabc.Sequence[dict[str, object]],
    *,
    total_count: int,
) -> bytes:
    """Render one commit's check evidence as deterministic JSON bytes.

    `total_count` is recorded beside the projected runs, so a reader can see
    that the observation was complete without trusting the length of the list.
    """
    document = {"commit": commit, "total_count": total_count, "checks": list(checks)}
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")
