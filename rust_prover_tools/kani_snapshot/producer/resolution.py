"""Resolution policy for the upstream commit a snapshot may be built from.

Two routes select a commit: a manual dispatch input and the newest upstream
release whose required Linux checks passed. Both fail closed. A manual input is
an identity, never a ref or a command line. A CI candidate qualifies only when
every required check has a completed, successful run; a newer run of the same
check supersedes an older one, and two runs that cannot be ordered are refused
rather than guessed.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import require_full_sha

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "REQUIRED_UPSTREAM_CHECKS",
    "CandidateCommit",
    "CheckRun",
    "candidate_from_api",
    "resolve_manual_commit",
    "select_newest_candidate",
)

#: Upstream checks that must pass for the x86_64 Linux payloads.
#:
#: The bundle is built on ubuntu-22.04, and both glibc baselines are exercised,
#: so the 22.04 and 24.04 bundle, setup and regression jobs are all required.
#: macOS and aarch64 jobs are deliberately excluded: this project publishes
#: only `x86_64-unknown-linux-gnu`, and a macOS-only failure must not block it.
REQUIRED_UPSTREAM_CHECKS: typ.Final[tuple[str, ...]] = (
    "BuildBundle-Linux-x86_64",
    "TestBundle (ubuntu-22.04)",
    "TestBundle (ubuntu-24.04)",
    "TestLocalToolchain (ubuntu-22.04)",
    "TestLocalToolchain (ubuntu-24.04)",
    "regression (ubuntu-22.04)",
    "regression (ubuntu-24.04)",
)


@dc.dataclass(frozen=True, slots=True)
class CheckRun:
    """One observed upstream check run.

    Attributes
    ----------
    name : str
        Check name, such as `TestBundle (ubuntu-24.04)`.
    status : str
        GitHub status, such as `completed` or `in_progress`.
    conclusion : str | None
        Conclusion once completed; `None` while the run is pending.
    run_id : int
        Workflow run identifier, used only to order two runs of one check.
    started_at : str
        ISO-8601 UTC start time, used to pick the newest run of one check.
    """

    name: str
    status: str
    conclusion: str | None
    run_id: int = 0
    started_at: str = ""


@dc.dataclass(frozen=True, slots=True)
class CandidateCommit:
    """An upstream release commit and the checks observed on it.

    Attributes
    ----------
    commit : str
        Full 40-hex upstream commit.
    tag : str
        Upstream release tag naming that commit.
    published_at : str
        Release publication time in ISO-8601 UTC; newer is preferred.
    checks : tuple[CheckRun, ...]
        Every check run observed for the commit.
    """

    commit: str
    tag: str
    published_at: str
    checks: tuple[CheckRun, ...]


def resolve_manual_commit(value: str) -> str:
    """Validate a manual dispatch `upstream_commit` input.

    Parameters
    ----------
    value : str
        Raw workflow dispatch input.

    Returns
    -------
    str
        The full lowercase commit.

    Raises
    ------
    ProverToolError
        Raised when the input is not a full lowercase 40-hex commit. Branch
        names, tags, URLs and option-looking strings are all refused, so the
        value can never change what Git fetches.
    """
    return require_full_sha(value, "workflow_dispatch upstream_commit")


def _newest(checks: cabc.Sequence[CheckRun]) -> CheckRun:
    """Return the newest run, refusing an unordered pair."""
    ordered = sorted(checks, key=lambda run: (run.started_at, run.run_id))
    newest = ordered[-1]
    if len(ordered) > 1:
        previous = ordered[-2]
        same_order = (previous.started_at, previous.run_id) == (
            newest.started_at,
            newest.run_id,
        )
        different_result = (previous.status, previous.conclusion) != (
            newest.status,
            newest.conclusion,
        )
        if same_order and different_result:
            msg = (
                f"upstream check {newest.name!r} is ambiguous: two runs share "
                "an identity but report different results"
            )
            raise ProverToolError(msg)
    return newest


def _qualifies(checks: tuple[CheckRun, ...]) -> bool:
    """Say whether every required check's newest run succeeded."""
    observed: dict[str, list[CheckRun]] = {}
    for run in checks:
        observed.setdefault(run.name, []).append(run)
    for name in REQUIRED_UPSTREAM_CHECKS:
        runs = observed.get(name)
        if not runs:
            return False
        newest = _newest(runs)
        if newest.status != "completed" or newest.conclusion != "success":
            return False
    return True


def select_newest_candidate(candidates: cabc.Sequence[CandidateCommit]) -> str:
    """Return the newest upstream commit whose required checks all passed.

    Parameters
    ----------
    candidates : collections.abc.Sequence of CandidateCommit
        Observed release commits.

    Returns
    -------
    str
        Full 40-hex commit of the selected candidate.

    Raises
    ------
    ProverToolError
        Raised when a commit is malformed or repeated, when a required check
        has unordered conflicting runs, or when no candidate qualifies.
    """
    commits = [
        require_full_sha(candidate.commit, "upstream candidate commit")
        for candidate in candidates
    ]
    repeated = sorted({commit for commit in commits if commits.count(commit) > 1})
    if repeated:
        msg = f"upstream candidates repeat commit {repeated[0]}"
        raise ProverToolError(msg)
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.published_at, candidate.tag),
        reverse=True,
    )
    for candidate in ordered:
        if _qualifies(candidate.checks):
            return candidate.commit
    msg = "no qualified upstream candidate has every required Linux check"
    raise ProverToolError(msg)


def _field(document: cabc.Mapping[str, object], key: str, field: str) -> object:
    """Return a required key from an API object."""
    if key not in document:
        msg = f"upstream {field} is missing {key}"
        raise ProverToolError(msg)
    return document[key]


def _text(document: cabc.Mapping[str, object], key: str, field: str) -> str:
    """Return a required string from an API object."""
    value = _field(document, key, field)
    if not isinstance(value, str):
        msg = f"upstream {field} {key} must be a string"
        raise ProverToolError(msg)
    return value


def _check_run(document: object) -> CheckRun:
    """Parse one check run from the GitHub API."""
    if not isinstance(document, dict):
        msg = "upstream check run must be an object"
        raise ProverToolError(msg)
    record = typ.cast("cabc.Mapping[str, object]", document)
    run_id = _field(record, "id", "check run")
    conclusion = _field(record, "conclusion", "check run")
    status = _text(record, "status", "check run")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        msg = "upstream check run id must be an integer"
        raise ProverToolError(msg)
    if conclusion is not None and not isinstance(conclusion, str):
        msg = "upstream check run conclusion must be a string or null"
        raise ProverToolError(msg)
    return CheckRun(
        name=_text(record, "name", "check run"),
        status=status,
        conclusion=conclusion,
        run_id=run_id,
        started_at=_text(record, "started_at", "check run"),
    )


def candidate_from_api(
    release: cabc.Mapping[str, object],
    check_runs: cabc.Sequence[object],
) -> CandidateCommit:
    """Build a candidate from a release object and its check runs.

    Parameters
    ----------
    release : collections.abc.Mapping of str to object
        GitHub release object; draft and prerelease releases are refused.
    check_runs : collections.abc.Sequence of object
        Check-run objects returned for the release's commit.

    Returns
    -------
    CandidateCommit
        The parsed candidate.

    Raises
    ------
    ProverToolError
        Raised when the release is a draft or prerelease, names no full commit,
        or a check run is malformed.
    """
    for flag in ("draft", "prerelease"):
        if _field(release, flag, "release") is not False:
            msg = f"upstream release must not be a {flag}"
            raise ProverToolError(msg)
    return CandidateCommit(
        commit=require_full_sha(
            _field(release, "commit", "release"), "upstream release commit"
        ),
        tag=_text(release, "tag", "release"),
        published_at=_text(release, "published_at", "release"),
        checks=tuple(_check_run(run) for run in check_runs),
    )
