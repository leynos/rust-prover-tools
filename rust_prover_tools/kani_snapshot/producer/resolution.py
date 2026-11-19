"""Resolution policy for the upstream commit a snapshot may be built from.

Two routes select a commit: a manual dispatch input and the newest upstream
release whose required Linux checks passed. Both fail closed. A manual input is
an identity, never a ref or a command line. A CI candidate qualifies only when
every required check has a completed, successful run; a newer run of the same
check supersedes an older one, and runs that share an identity but disagree are
refused rather than guessed. A run that has not started yet has no start time,
so it is treated as the newest observation of its check: a queued rerun blocks
qualification until it reports something.

The shapes and API parsing these rules read live in `candidate_api`; this
module is only the policy over them.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.candidate_api import (
    CandidateCommit,
    CheckRun,
    candidate_from_api,
    parse_check_run,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_mapping,
    require_non_negative_int,
    require_sequence,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "REQUIRED_UPSTREAM_CHECKS",
    "CandidateCommit",
    "CheckRun",
    "candidate_from_api",
    "parse_commit_checks",
    "require_required_checks",
    "resolve_manual_commit",
    "select_newest_candidate",
)

#: Keys of the check evidence a manual dispatch must present for its commit.
_COMMIT_CHECK_KEYS: typ.Final[frozenset[str]] = frozenset({
    "commit",
    "total_count",
    "checks",
})

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


def _check_failures(checks: tuple[CheckRun, ...]) -> list[str]:
    """Return one reason per required check that has not passed.

    Ambiguity between tied runs is a different failure from a check that has
    not passed, so `_newest` still raises for it rather than being reported as
    a missing success.
    """
    observed: dict[str, list[CheckRun]] = {}
    for run in checks:
        observed.setdefault(run.name, []).append(run)
    failures: list[str] = []
    for name in REQUIRED_UPSTREAM_CHECKS:
        runs = observed.get(name)
        if not runs:
            failures.append(f"{name}: no run was observed")
            continue
        newest = _newest(runs)
        if newest.status != "completed" or newest.conclusion != "success":
            failures.append(f"{name}: {newest.status}/{newest.conclusion}")
    return failures


def _qualifies(checks: tuple[CheckRun, ...]) -> bool:
    """Say whether every required check's newest run succeeded."""
    return not _check_failures(checks)


def require_required_checks(
    checks: cabc.Sequence[CheckRun],
    *,
    subject: str,
) -> None:
    """Require every required upstream check's newest run to have passed.

    Parameters
    ----------
    checks : collections.abc.Sequence of CheckRun
        Check runs observed for one commit.
    subject : str
        What the commit is, for an actionable refusal.

    Raises
    ------
    ProverToolError
        Raised when any required check is missing or did not succeed, or when
        tied runs of one check report different results.
    """
    failures = _check_failures(tuple(checks))
    if not failures:
        return
    msg = f"{subject} has not passed every required upstream check: " + "; ".join(
        failures
    )
    raise ProverToolError(msg)


def parse_commit_checks(data: bytes) -> str:
    """Parse one dispatched commit's check evidence and require it to pass.

    A manual dispatch names the commit it wants built. The design requires the
    same upstream checks a scheduled candidate must pass, so the dispatch is
    only accepted with observed evidence for that commit rather than on the
    strength of a well-formed SHA.

    Parameters
    ----------
    data : bytes
        Exact document bytes: `{"commit": <40-hex>, "checks": [...]}`.

    Returns
    -------
    str
        The full commit the evidence is about, once every required check has
        passed.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, an unknown or missing key, a malformed commit
        or check run, or a required check that has not passed.
    """
    document = require_mapping(
        strict_json_loads(data, "manual check evidence"), "manual check evidence"
    )
    reject_unknown_keys(document, _COMMIT_CHECK_KEYS, "manual check evidence")
    missing = sorted(_COMMIT_CHECK_KEYS - set(document))
    if missing:
        msg = f"manual check evidence is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    commit = require_full_sha(document["commit"], "manual check evidence commit")
    observed = require_sequence(document["checks"], "manual check evidence checks")
    total = require_non_negative_int(
        document["total_count"], "manual check evidence total_count"
    )
    if total != len(observed):
        msg = (
            f"manual check evidence records {len(observed)} of {total} check "
            "runs; an incomplete observation cannot prove the commit qualified"
        )
        raise ProverToolError(msg)
    checks = tuple(parse_check_run(run) for run in observed)
    require_required_checks(checks, subject=f"upstream commit {commit}")
    return commit


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
