"""The candidate and check shapes the resolution policy reasons about.

Resolution policy lives in `resolution`; this module only turns the GitHub API
objects the resolve job collects into the validated shapes that policy reads.
Every field is required and validated here, so a malformed API response is
refused before it can influence which commit is selected.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import require_full_sha

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "CandidateCommit",
    "CheckRun",
    "candidate_from_api",
    "parse_check_run",
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


def parse_check_run(document: object) -> CheckRun:
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
        checks=tuple(parse_check_run(run) for run in check_runs),
    )
