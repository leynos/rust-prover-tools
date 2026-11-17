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

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.collect_candidates import (
    UPSTREAM_REPOSITORY,
    collect_candidates,
    main,
)
from rust_prover_tools.kani_snapshot.producer.entrypoint import main as entrypoint
from rust_prover_tools.kani_snapshot.producer.lifecycle import parse_resolution
from rust_prover_tools.kani_snapshot.producer.resolution import (
    REQUIRED_UPSTREAM_CHECKS,
)
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
    "?per_page=100"
)


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


def _checks() -> dict:
    """Return one successful run per required upstream check."""
    return {
        "total_count": len(REQUIRED_UPSTREAM_CHECKS),
        "check_runs": [
            {
                "id": index + 1,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-09-16T03:00:00Z",
                "output": {"title": "not copied"},
            }
            for index, name in enumerate(REQUIRED_UPSTREAM_CHECKS)
        ],
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

    document = json.loads(collect_candidates())

    assert len(document) == 1
    candidate = document[0]
    assert candidate["release"] == {
        "tag_name": TAG,
        "published_at": "2026-09-16T03:43:14Z",
        "draft": False,
        "prerelease": False,
    }
    assert candidate["tag_ref"]["object"]["sha"] == fixtures.UPSTREAM_COMMIT
    assert [run["name"] for run in candidate["checks"]] == list(
        REQUIRED_UPSTREAM_CHECKS
    )
    assert "output" not in candidate["checks"][0]


def test_draft_and_prerelease_releases_are_skipped(cmd_mox: CmdMox) -> None:
    """A release that is not public cannot supply a build input."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release(draft=True), _release(prerelease=True)],
        })
    )

    with pytest.raises(ProverToolError, match="no stable"):
        collect_candidates()


def test_a_tag_ref_that_is_not_a_full_commit_is_refused(cmd_mox: CmdMox) -> None:
    """The commit is interpolated into a URL, so it is validated first."""
    cmd_mox.stub("gh").runs(
        _serve({
            RELEASES: [_release()],
            TAG_REF: _tag_ref("../../other"),
        })
    )

    with pytest.raises(ProverToolError, match="full lowercase"):
        collect_candidates()


def test_a_failed_api_call_is_refused(cmd_mox: CmdMox) -> None:
    """A read that could not be completed is not evidence of anything."""

    def fail(invocation: Invocation) -> tuple[str, str, int]:
        """Answer any call with a server failure."""
        return ("", "gh: HTTP 500: Internal Server Error", 1)

    cmd_mox.stub("gh").runs(fail)

    with pytest.raises(ProverToolError, match="gh api failed"):
        collect_candidates()


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
