"""Collect upstream release and check-run evidence for the scheduled route.

Resolution is pure policy over observed evidence, so something has to observe.
That is this module: it asks the upstream repository for its recent releases,
the tag ref each release names, and the check runs on that commit, then writes
the one document `resolve --route scheduled` consumes. It runs in the resolve
job with read-only credentials and no access to the producer's release.

This module owns fetching only. `upstream_evidence` owns what a response may
contribute: the projection into the fields resolution reads, and the
deterministic rendering of the document. Keeping the two apart means a change
to the request a job makes cannot silently change what it records, and the
commit is validated as a full SHA before it is placed in a URL, so a hostile
or simply unexpected API response cannot redirect a later request.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_transport import GhRunner, gh
from rust_prover_tools.kani_snapshot.orchestrator.documents import write_document
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.upstream_evidence import (
    UPSTREAM_TAG_PREFIX,
    candidates_to_json,
    checks_to_json,
    project_check_page,
    project_release,
)
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_mapping,
    require_sequence,
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

#: Releases considered per run; older ones have long since been superseded.
_RELEASE_LIMIT: typ.Final[int] = 10

#: Check runs requested per page.
_CHECK_PAGE_SIZE: typ.Final[int] = 100

#: Pages one commit's check runs may occupy. Reaching it is a failure, because
#: a truncated observation would silently drop evidence resolution depends on.
_CHECK_PAGE_LIMIT: typ.Final[int] = 10


def _gh_json(run: GhRunner, arguments: cabc.Sequence[str], field: str) -> object:
    """Run one `gh api` call and parse its output strictly.

    The failure kind is preserved so a transient transport failure stays
    distinguishable from a permanent refusal: only a transient one may be
    reconciled by collecting the evidence again.
    """
    try:
        output = run(("gh", "api", *arguments))
    except ReleaseTransientError as exc:
        msg = f"gh api failed for {field}: {exc}"
        raise ReleaseTransientError(msg) from exc
    except ProverToolError as exc:
        msg = f"gh api failed for {field}: {exc}"
        raise ProverToolError(msg) from exc
    return strict_json_loads(output.encode("utf-8"), field)


def _check_page(
    run: GhRunner,
    commit: str,
    page: int,
) -> tuple[int, list[dict[str, object]]]:
    """Read one page of a commit's check runs and its reported total."""
    document = _gh_json(
        run,
        [
            (
                f"repos/{UPSTREAM_REPOSITORY}/commits/{commit}/check-runs"
                f"?per_page={_CHECK_PAGE_SIZE}&page={page}"
            )
        ],
        f"upstream check runs for {commit}",
    )
    return project_check_page(document, commit)


def _check_runs(
    run: GhRunner,
    commit: str,
) -> tuple[int, list[dict[str, object]]]:
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
        total, runs = _check_page(run, commit, page)
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


def _candidate(
    run: GhRunner,
    release: dict[str, object],
) -> dict[str, object]:
    """Collect one usable release's tag ref and check runs."""
    tag = typ.cast("str", release["tag_name"])
    tag_ref = _gh_json(
        run,
        [f"repos/{UPSTREAM_REPOSITORY}/git/ref/tags/{tag}"],
        f"upstream tag ref {tag}",
    )
    reference = require_mapping(tag_ref, f"upstream tag ref {tag}")
    target = require_mapping(reference.get("object"), f"upstream tag ref {tag} object")
    commit = require_full_sha(target.get("sha"), f"upstream tag ref {tag} commit")
    total, checks = _check_runs(run, commit)
    return {
        "release": release,
        "tag_ref": reference,
        "total_count": total,
        "checks": checks,
    }


def collect_candidates(*, run: GhRunner) -> bytes:
    """Collect the candidate document the scheduled resolution consumes.

    Parameters
    ----------
    run : GhRunner
        Transport that performs one `gh` invocation. The command entrypoint
        supplies the real one; a test supplies a recorder.

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
        run,
        [f"repos/{UPSTREAM_REPOSITORY}/releases?per_page={_RELEASE_LIMIT}"],
        "upstream release list",
    )
    candidates: list[dict[str, object]] = []
    for release in require_sequence(releases, "upstream release list"):
        projected = project_release(release)
        if projected is not None:
            candidates.append(_candidate(run, projected))
    if not candidates:
        msg = f"no stable {UPSTREAM_TAG_PREFIX} release was found upstream"
        raise ProverToolError(msg)
    return candidates_to_json(candidates)


def collect_commit_checks(commit: str, *, run: GhRunner) -> bytes:
    """Collect the observed check runs for one dispatched commit.

    A manual dispatch must present the same upstream-check evidence a scheduled
    candidate does. The commit is validated before it is placed in a URL, so a
    hostile dispatch input cannot redirect the request.

    Parameters
    ----------
    commit : str
        Full lowercase 40-hex upstream commit.
    run : GhRunner
        Transport that performs one `gh` invocation.

    Returns
    -------
    bytes
        Deterministic JSON
        `{"commit": <commit>, "total_count": <int>, "checks": [...]}` document.
        `total_count` records how many runs the API observed, so a reader can
        distinguish a complete observation from a truncated `checks` list; the
        resolver requires it.

    Raises
    ------
    ProverToolError
        Raised when the commit is malformed, the API fails, or a response is
        not the expected shape.
    """
    resolved = require_full_sha(commit, "workflow_dispatch upstream_commit")
    total, checks = _check_runs(run, resolved)
    return checks_to_json(resolved, checks, total_count=total)


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
            document = collect_commit_checks(namespace.commit, run=gh)
        else:
            document = collect_candidates(run=gh)
        write_document(pathlib.Path(namespace.output), document)
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-candidates: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
