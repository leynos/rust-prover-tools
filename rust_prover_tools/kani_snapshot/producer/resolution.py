"""Resolution policy for the upstream commit a snapshot may be built from.

Two routes select a commit: a manual dispatch input and the newest upstream
release whose required Linux checks passed. Both fail closed. A manual input is
an identity, never a ref or a command line. A CI candidate qualifies only when
every required check has a completed, successful run; a newer run of the same
check supersedes an older one, and runs that share an identity but disagree are
refused rather than guessed. A run that has not started yet has no start time,
so it is treated as the newest observation of its check: a queued rerun blocks
qualification until it reports something.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
)

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
    started_at : str | None
        ISO-8601 UTC start time, used to pick the newest run of one check;
        `None` while the run is queued, which GitHub reports as `null`.
    """

    name: str
    status: str
    conclusion: str | None
    run_id: int = 0
    started_at: str | None = None


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


def _identity(run: CheckRun) -> tuple[str, int]:
    """Return the value that says which observation a run is.

    An unstarted run has no time; it still carries a run identifier, so two
    queued runs stay distinguishable.
    """
    return (run.started_at or "", run.run_id)


def _order(run: CheckRun) -> tuple[bool, str, int]:
    """Return a sort key in which an unstarted run counts as the newest.

    A queued rerun is newer evidence than the success it will replace, so it
    must supersede it rather than be ignored. Ordering it last also keeps the
    key free of `None`, which cannot be compared with a timestamp.
    """
    return (run.started_at is None, run.started_at or "", run.run_id)


def _newest(checks: cabc.Sequence[CheckRun]) -> CheckRun:
    """Return the newest run, refusing any tie that reports different results.

    Every run sharing the newest identity is compared, not just the final pair
    of a sort: with three tied observations, two matching successes must not
    hide a conflicting failure.
    """
    newest = max(checks, key=_order)
    identity = _identity(newest)
    results = {
        (run.status, run.conclusion) for run in checks if _identity(run) == identity
    }
    if len(results) > 1:
        msg = (
            f"upstream check {newest.name!r} is ambiguous: runs share an "
            "identity but report different results"
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
    started_at = _field(record, "started_at", "check run")
    if started_at is not None and not isinstance(started_at, str):
        msg = "upstream check run started_at must be a string or null"
        raise ProverToolError(msg)
    return CheckRun(
        name=_text(record, "name", "check run"),
        status=status,
        conclusion=conclusion,
        run_id=run_id,
        started_at=started_at,
    )


def _released_commit(tag_ref: cabc.Mapping[str, object], tag: str) -> str:
    """Return the commit a release tag ref resolves to.

    A release object's `target_commitish` is *not* the released commit: GitHub
    copies whatever the release form held, which for Kani is the branch name
    `main`. The commit is therefore taken from the tag ref instead, and an
    annotated tag is refused rather than silently recorded as its own tag
    object, because only the dereferenced commit can be built.
    """
    expected = f"refs/tags/{tag}"
    reference = _text(tag_ref, "ref", "tag ref")
    if reference != expected:
        msg = f"upstream tag ref {reference!r} does not name {expected!r}"
        raise ProverToolError(msg)
    target = _field(tag_ref, "object", "tag ref")
    if not isinstance(target, dict):
        msg = "upstream tag ref object must be an object"
        raise ProverToolError(msg)
    record = typ.cast("cabc.Mapping[str, object]", target)
    kind = _text(record, "type", "tag ref object")
    if kind != "commit":
        msg = (
            f"upstream tag ref {expected!r} points at a {kind!r}, not a commit; "
            "dereference an annotated tag before resolving it"
        )
        raise ProverToolError(msg)
    return require_full_sha(
        _field(record, "sha", "tag ref object"), "upstream release commit"
    )


def candidate_from_api(
    release: cabc.Mapping[str, object],
    tag_ref: cabc.Mapping[str, object],
    check_runs: cabc.Sequence[object],
) -> CandidateCommit:
    """Build a candidate from a release, its tag ref and its check runs.

    The arguments are GitHub API objects as returned, not a normalised form:
    the release supplies `tag_name` and `published_at`, and the separately
    fetched tag ref supplies the commit the tag names.

    Parameters
    ----------
    release : collections.abc.Mapping of str to object
        GitHub release object; draft and prerelease releases are refused.
    tag_ref : collections.abc.Mapping of str to object
        GitHub `git/ref` object for `refs/tags/<tag_name>`.
    check_runs : collections.abc.Sequence of object
        Check-run objects returned for the release's commit.

    Returns
    -------
    CandidateCommit
        The parsed candidate.

    Raises
    ------
    ProverToolError
        Raised when the release is a draft or prerelease, the tag ref does not
        name the release's tag or does not resolve to a commit, or a check run
        is malformed.
    """
    for flag in ("draft", "prerelease"):
        if _field(release, flag, "release") is not False:
            msg = f"upstream release must not be a {flag}"
            raise ProverToolError(msg)
    tag = _text(release, "tag_name", "release")
    return CandidateCommit(
        commit=_released_commit(tag_ref, tag),
        tag=tag,
        published_at=_text(release, "published_at", "release"),
        checks=tuple(_check_run(run) for run in check_runs),
    )
