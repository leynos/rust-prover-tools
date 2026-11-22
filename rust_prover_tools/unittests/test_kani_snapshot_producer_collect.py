"""Scheduled resolution needs evidence, and collecting it is a boundary.

The collector runs with read-only credentials and turns three GitHub API
responses into the one document resolution parses. These tests pin the
projection: only stable `kani-` releases are candidates, only the fields
resolution reads are written, and a tag ref that does not name a full commit is
refused before its string reaches a URL.
"""

from __future__ import annotations

import json
import typing as typ
from itertools import starmap

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_transport import GhRunner, gh
from rust_prover_tools.kani_snapshot.orchestrator.collect_candidates import (
    UPSTREAM_REPOSITORY,
    collect_candidates,
    collect_commit_checks,
    main,
)
from rust_prover_tools.kani_snapshot.orchestrator.entrypoint import main as entrypoint
from rust_prover_tools.kani_snapshot.producer.lifecycle import parse_resolution
from rust_prover_tools.kani_snapshot.producer.resolution import (
    REQUIRED_UPSTREAM_CHECKS,
)
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)

PINNED = fixtures.pinned_recipe()
TAG = PINNED.upstream.tag
RELEASES = f"repos/{UPSTREAM_REPOSITORY}/releases?per_page=10"
TAG_REF = f"repos/{UPSTREAM_REPOSITORY}/git/ref/tags/{TAG}"
CHECKS = (
    f"repos/{UPSTREAM_REPOSITORY}/commits/{fixtures.UPSTREAM_COMMIT}/check-runs"
    "?per_page=100&page=1"
)
CHECKS_PAGE_2 = CHECKS.removesuffix("&page=1") + "&page=2"
NOTICES_URL: typ.Final[str] = "https://github.com/kani/kani/actions/runs/9001"

#: A failure message an unreachable server would produce.
_SERVER_UNAVAILABLE: typ.Final[str] = "HTTP 503: Service Unavailable"


def _release(*, tag: str = TAG, draft: bool = False, prerelease: bool = False) -> dict:
    """Return one release as the API serves it."""
    return {
        "tag_name": tag,
        "published_at": "2026-09-16T03:43:14Z",
        "draft": draft,
        "prerelease": prerelease,
        "body": "release notes the collector must not copy",
    }


def _tag_ref(sha: str = fixtures.UPSTREAM_COMMIT) -> dict:
    """Return the tag ref for the pinned release."""
    return {
        "ref": f"refs/tags/{TAG}",
        "object": {"type": "commit", "sha": sha},
        "url": "https://api.github.com/ignored",
    }


def _run(index: int, name: str, *, head: str = fixtures.UPSTREAM_COMMIT) -> dict:
    """Return one successful check run as the API serves it."""
    return {
        "id": index + 1,
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-16T03:00:00Z",
        "head_sha": head,
        "html_url": f"{NOTICES_URL}/{index}",
        "details_url": f"https://api.github.com/ignored/{index}",
        "output": {"title": "not copied"},
    }


def _checks() -> dict:
    """Return one successful run per required upstream check."""
    return {
        "total_count": len(REQUIRED_UPSTREAM_CHECKS),
        "check_runs": list(starmap(_run, enumerate(REQUIRED_UPSTREAM_CHECKS))),
    }


def _serve(
    github: dict[str, object],
) -> cabc.Callable[[Invocation], tuple[str, str, int]]:
    """Return a `gh` handler answering from a path-to-document map."""

    def handle(invocation: Invocation) -> tuple[str, str, int]:
        """Answer one API call, recording unexpected ones as failures."""
        arguments = list(invocation.args)
        if arguments[:1] != ["api"] or arguments[1] not in github:
            msg = f"unexpected gh call: {arguments}"
            raise AssertionError(msg)
        return (json.dumps(github[arguments[1]]), "", 0)

    return handle


def test_the_collector_composes_one_candidate_per_stable_release(
    cmd_mox: CmdMox,
) -> None:
    """Only the fields resolution reads are carried into the document."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release(), _release(tag="v1.2.3", prerelease=True)],
            TAG_REF: _tag_ref(),
            CHECKS: _checks(),
        })
    )

    document = json.loads(collect_candidates(run=gh))

    assert len(document) == 1
    candidate = document[0]
    assert candidate["release"] == {
        "tag_name": TAG,
        "published_at": "2026-09-16T03:43:14Z",
        "draft": False,
        "prerelease": False,
    }
    assert candidate["tag_ref"]["object"]["sha"] == fixtures.UPSTREAM_COMMIT
    assert candidate["total_count"] == len(REQUIRED_UPSTREAM_CHECKS)
    assert [run["name"] for run in candidate["checks"]] == list(
        REQUIRED_UPSTREAM_CHECKS
    )
    assert "output" not in candidate["checks"][0]
    assert candidate["checks"][0]["head_sha"] == fixtures.UPSTREAM_COMMIT
    assert candidate["checks"][0]["html_url"] == f"{NOTICES_URL}/0"


def test_every_check_run_page_is_collected(cmd_mox: CmdMox) -> None:
    """Completeness is the reported total, not the length of the first page."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref(),
            CHECKS: {"total_count": 3, "check_runs": [_run(0, "a"), _run(1, "b")]},
            CHECKS_PAGE_2: {"total_count": 3, "check_runs": [_run(2, "c")]},
        })
    )

    document = json.loads(collect_candidates(run=gh))

    candidate = document[0]
    assert candidate["total_count"] == 3
    assert [run["name"] for run in candidate["checks"]] == ["a", "b", "c"]


def test_a_truncated_check_observation_is_refused(cmd_mox: CmdMox) -> None:
    """An empty page before the reported total is not a complete observation."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref(),
            CHECKS: {"total_count": 5, "check_runs": [_run(0, "a")]},
            CHECKS_PAGE_2: {"total_count": 5, "check_runs": []},
        })
    )

    with pytest.raises(ProverToolError, match="was empty before"):
        collect_candidates(run=gh)


def test_a_check_run_about_another_commit_is_refused(cmd_mox: CmdMox) -> None:
    """A run for another commit is not evidence about the released one."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref(),
            CHECKS: {"total_count": 1, "check_runs": [_run(0, "a", head="f" * 40)]},
        })
    )

    with pytest.raises(ProverToolError, match="not the queried commit"):
        collect_candidates(run=gh)


def test_manual_check_evidence_records_its_total(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """A dispatch's evidence carries the reported total beside the runs."""
    cmd_mox.stub("gh").runs(_serve({CHECKS: _checks()}))
    output = tmp_path / "checks.json"

    assert main(["--commit", fixtures.UPSTREAM_COMMIT, "--output", str(output)]) == 0

    document = json.loads(output.read_bytes())
    assert document["commit"] == fixtures.UPSTREAM_COMMIT
    assert document["total_count"] == len(REQUIRED_UPSTREAM_CHECKS)
    assert len(document["checks"]) == len(REQUIRED_UPSTREAM_CHECKS)


def test_draft_and_prerelease_releases_are_skipped(cmd_mox: CmdMox) -> None:
    """A release that is not public cannot supply a build input."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release(draft=True), _release(prerelease=True)],
        })
    )

    with pytest.raises(ProverToolError, match="no stable"):
        collect_candidates(run=gh)


def test_a_tag_ref_that_is_not_a_full_commit_is_refused(cmd_mox: CmdMox) -> None:
    """The commit is interpolated into a URL, so it is validated first."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref("../../other"),
        })
    )

    with pytest.raises(ProverToolError, match="full lowercase"):
        collect_candidates(run=gh)


def test_a_failed_api_call_is_refused(cmd_mox: CmdMox) -> None:
    """A read that could not be completed is not evidence of anything."""

    def fail(invocation: Invocation) -> tuple[str, str, int]:
        """Answer any call with a server failure."""
        return ("", "gh: HTTP 500: Internal Server Error", 1)

    cmd_mox.stub("gh").runs(fail)

    with pytest.raises(ProverToolError, match="gh api failed"):
        collect_candidates(run=gh)


def _record(
    github: dict[str, object],
) -> tuple[list[tuple[str, ...]], GhRunner]:
    """Return a recording runner answering from a path-to-document map."""
    calls: list[tuple[str, ...]] = []

    def record(
        argv: cabc.Sequence[str], *, timeout_seconds: float | None = None
    ) -> str:
        """Record the request and answer it from the map."""
        calls.append(tuple(argv))
        return json.dumps(github[argv[2]])

    return calls, record


def test_the_scheduled_route_reads_release_tag_and_checks_in_order() -> None:
    """The request sequence itself is asserted, without a process boundary."""
    calls, record = _record({
        RELEASES: [_release()],
        TAG_REF: _tag_ref(),
        CHECKS: _checks(),
    })

    collect_candidates(run=record)

    assert calls == [
        ("gh", "api", RELEASES),
        ("gh", "api", TAG_REF),
        ("gh", "api", CHECKS),
    ]


def test_the_manual_route_reads_only_the_dispatched_commits_checks() -> None:
    """A dispatch is evidence about one commit, so no release list is read."""
    calls, record = _record({CHECKS: _checks()})

    collect_commit_checks(fixtures.UPSTREAM_COMMIT, run=record)

    assert calls == [("gh", "api", CHECKS)]


def test_a_transient_transport_failure_stays_transient() -> None:
    """Naming the failed read must not reclassify a retryable failure."""

    def unavailable(
        argv: cabc.Sequence[str], *, timeout_seconds: float | None = None
    ) -> str:
        """Fail the way an unreachable server does."""
        raise ReleaseTransientError(_SERVER_UNAVAILABLE)

    with pytest.raises(ReleaseTransientError, match="upstream release list"):
        collect_candidates(run=unavailable)


def test_the_command_writes_the_candidate_document(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The resolve job runs a command, not an imported function."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref(),
            CHECKS: _checks(),
        })
    )
    output = tmp_path / "candidates.json"

    assert main(["--output", str(output)]) == 0

    assert len(json.loads(output.read_bytes())) == 1


def test_collected_evidence_drives_a_scheduled_resolution(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The collector's document is exactly what `resolve` accepts."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref(),
            CHECKS: _checks(),
        })
    )
    candidates = tmp_path / "candidates.json"
    assert main(["--output", str(candidates)]) == 0

    assert (
        entrypoint([
            "resolve",
            "--route",
            "scheduled",
            "--candidates",
            str(candidates),
            "--output",
            str(tmp_path / "resolution.json"),
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--event",
            "schedule",
        ])
        == 0
    )
    resolution = parse_resolution((tmp_path / "resolution.json").read_bytes())
    assert resolution.upstream_commit == fixtures.UPSTREAM_COMMIT
    assert resolution.upstream_tag == TAG
