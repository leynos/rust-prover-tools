"""Collect upstream release and check-run evidence for the scheduled route.

Resolution is pure policy over observed evidence, so something has to observe.
That is this module: it asks the upstream repository for its recent releases,
the tag ref each release names, and the check runs on that commit, then writes
the one document `resolve --route scheduled` consumes. It runs in the resolve
job with read-only credentials and no access to the producer's release.

Only the fields resolution reads are projected into the document, and the
commit is validated as a full SHA before it is placed in a URL, so a hostile or
simply unexpected API response cannot redirect a later request.
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
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
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
    "candidates_to_json",
    "checks_to_json",
    "collect_candidates",
    "collect_commit_checks",
    "main",
)

#: Upstream repository whose releases a Kani snapshot may be built from.
#:
#: Taken from the pinned recipe rather than repeated here: resolution can only
#: ever select that recipe's commit, so collecting from any other repository
#: would produce evidence no build could use.
UPSTREAM_REPOSITORY: typ.Final[str] = pinned_recipe().upstream.repository

#: Only a release tagged this way is a Kani release.
UPSTREAM_TAG_PREFIX: typ.Final[str] = "kani-"

#: Releases considered per run; older ones have long since been superseded.
_RELEASE_LIMIT: typ.Final[int] = 10

#: Check runs requested per page.
_CHECK_PAGE_SIZE: typ.Final[int] = 100

#: Pages one commit's check runs may occupy. Reaching it is a failure, because
#: a truncated observation would silently drop evidence resolution depends on.
_CHECK_PAGE_LIMIT: typ.Final[int] = 10


def _gh_json(arguments: cabc.Sequence[str], field: str) -> object:
    """Run one `gh api` call and parse its output strictly."""
    result = run_command(CommandSpec(("gh", "api", *arguments)))
    if result.exit_code != 0:
        msg = f"gh api failed for {field}: {result.stderr.strip()}"
        raise ProverToolError(msg)
    return strict_json_loads(result.stdout.encode("utf-8"), field)


def _release_document(release: object) -> dict[str, object] | None:
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


def _check_run(commit: str, run: object, index: int) -> dict[str, object]:
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


def _check_page(
    commit: str,
    page: int,
) -> tuple[int, list[dict[str, object]]]:
    """Read one page of a commit's check runs and its reported total."""
    document = _gh_json(
        [
            (
                f"repos/{UPSTREAM_REPOSITORY}/commits/{commit}/check-runs"
                f"?per_page={_CHECK_PAGE_SIZE}&page={page}"
            )
        ],
        f"upstream check runs for {commit}",
    )
    record = require_mapping(document, "upstream check-run document")
    total = require_non_negative_int(
        record.get("total_count"), "upstream check-run total_count"
    )
    runs = [
        _check_run(commit, run, index)
        for index, run in enumerate(
            require_sequence(record.get("check_runs"), "upstream check runs")
        )
    ]
    return total, runs


def _check_runs(commit: str) -> tuple[int, list[dict[str, object]]]:
    """Collect every check run for `commit`, refusing a truncated answer.

    GitHub reports `total_count` beside one page of runs, so the count is what
    proves completeness: a single-page request used to treat the first 100 of
    a larger set as the whole observation. Collection stops only when the
    reported total has been reached, and reaching the page bound or an empty
    page first is an actionable failure rather than a smaller document.
    """
    collected: list[dict[str, object]] = []
    total: int | None = None
    for page in range(1, _CHECK_PAGE_LIMIT + 1):
        total, runs = _check_page(commit, page)
        collected += runs
        if len(collected) >= total:
            break
        if not runs:
            msg = (
                f"upstream check-run page {page} for {commit} was empty before "
                f"its {total} runs were observed"
            )
            raise ProverToolError(msg)
    else:
        msg = (
            f"upstream check runs for {commit} exceeded {_CHECK_PAGE_LIMIT} "
            f"pages; raise the bound deliberately after reviewing the commit"
        )
        raise ProverToolError(msg)
    if total is None or len(collected) != total:
        msg = (
            f"upstream check runs for {commit} observed {len(collected)} runs "
            f"of a reported {total}"
        )
        raise ProverToolError(msg)
    return total, collected


def _candidate(release: dict[str, object]) -> dict[str, object]:
    """Collect one usable release's tag ref and check runs."""
    tag = typ.cast("str", release["tag_name"])
    tag_ref = _gh_json(
        [f"repos/{UPSTREAM_REPOSITORY}/git/ref/tags/{tag}"],
        f"upstream tag ref {tag}",
    )
    reference = require_mapping(tag_ref, f"upstream tag ref {tag}")
    target = require_mapping(reference.get("object"), f"upstream tag ref {tag} object")
    commit = require_full_sha(target.get("sha"), f"upstream tag ref {tag} commit")
    total, checks = _check_runs(commit)
    return {
        "release": release,
        "tag_ref": reference,
        "total_count": total,
        "checks": checks,
    }


def collect_candidates() -> bytes:
    """Collect the candidate document the scheduled resolution consumes.

    Returns
    -------
    bytes
        Deterministic JSON array of
        `{release, tag_ref, total_count, checks}` objects, one per stable
        `kani-` release, newest first as the API returned them. Each check run
        keeps its head SHA and URLs, and `total_count` records how many runs
        the API reported.

    Raises
    ------
    ProverToolError
        Raised when the API fails, a response is not the expected shape, a tag
        ref does not name a full commit, or a commit's check-run observation
        cannot be completed within its page bound.
    """
    releases = _gh_json(
        [f"repos/{UPSTREAM_REPOSITORY}/releases?per_page={_RELEASE_LIMIT}"],
        "upstream release list",
    )
    candidates: list[dict[str, object]] = []
    for release in require_sequence(releases, "upstream release list"):
        projected = _release_document(release)
        if projected is not None:
            candidates.append(_candidate(projected))
    if not candidates:
        msg = f"no stable {UPSTREAM_TAG_PREFIX} release was found upstream"
        raise ProverToolError(msg)
    return candidates_to_json(candidates)


def candidates_to_json(candidates: cabc.Sequence[dict[str, object]]) -> bytes:
    """Render a candidate document as deterministic JSON bytes."""
    text = json.dumps(list(candidates), sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def collect_commit_checks(commit: str) -> bytes:
    """Collect the observed check runs for one dispatched commit.

    A manual dispatch must present the same upstream-check evidence a scheduled
    candidate does. The commit is validated before it is placed in a URL, so a
    hostile dispatch input cannot redirect the request.

    Parameters
    ----------
    commit : str
        Full lowercase 40-hex upstream commit.

    Returns
    -------
    bytes
        Deterministic JSON `{"commit": <commit>, "checks": [...]}` document.

    Raises
    ------
    ProverToolError
        Raised when the commit is malformed, the API fails, or a response is
        not the expected shape.
    """
    resolved = require_full_sha(commit, "workflow_dispatch upstream_commit")
    total, checks = _check_runs(resolved)
    return checks_to_json(resolved, checks, total_count=total)


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


def build_parser() -> argparse.ArgumentParser:
    """Return the candidate collector's command line parser."""
    parser = argparse.ArgumentParser(
        prog="kani-snapshot-candidates",
        description="Collect upstream release evidence for resolution.",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="collect check evidence for one dispatched commit instead",
    )
    parser.add_argument("--output", required=True)
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write the evidence document for one resolve job."""
    namespace = build_parser().parse_args(argv)
    try:
        if namespace.commit:
            document = collect_commit_checks(namespace.commit)
        else:
            document = collect_candidates()
        write_document(pathlib.Path(namespace.output), document)
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-candidates: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
