"""Identity allocation needs the releases that already exist.

An allocator that cannot see the newest tag will happily reuse it, so the
resolve job's release read is a correctness boundary rather than
bookkeeping. These tests pin the projection, the refusal to truncate, and the
fact that the collector's document is exactly what `identity` accepts.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.collect_releases import (
    RELEASE_LIMIT,
    collect_releases,
    main,
)
from rust_prover_tools.kani_snapshot.producer.entrypoint import main as entrypoint
from rust_prover_tools.kani_snapshot.producer.lifecycle import resolution_to_json
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    parse_publications,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)

RELEASE_LIST = [
    "release",
    "list",
    "--limit",
    str(RELEASE_LIMIT),
    "--json",
    "tagName,targetCommitish",
]


def _serve(
    releases: cabc.Sequence[dict[str, object]],
) -> cabc.Callable[[Invocation], tuple[str, str, int]]:
    """Return a `gh` handler serving one release list."""

    def handle(invocation: Invocation) -> tuple[str, str, int]:
        """Answer the release list, refusing any other call."""
        if list(invocation.args) != RELEASE_LIST:
            msg = f"unexpected gh call: {list(invocation.args)}"
            raise AssertionError(msg)
        return (json.dumps(list(releases)), "", 0)

    return handle


def test_the_collector_projects_only_the_identity_fields(
    cmd_mox: CmdMox,
) -> None:
    """A field identity allocation does not read is not carried forward."""
    cmd_mox.stub("gh").runs(
        _serve([
            {
                "tagName": fixtures.TAG,
                "targetCommitish": fixtures.BUILDER_COMMIT,
                "name": "notes the collector must not copy",
            }
        ])
    )

    document = json.loads(collect_releases())

    assert document == [
        {"tag": fixtures.TAG, "builder_commit": fixtures.BUILDER_COMMIT}
    ]


def test_an_exhausted_release_list_is_refused(cmd_mox: CmdMox) -> None:
    """A truncated view could hide the newest revision."""
    cmd_mox.stub("gh").runs(
        _serve([
            {"tagName": f"kani-snapshot-2026-01-{'a' * 12}-r1", "targetCommitish": ""}
            for _ in range(RELEASE_LIMIT)
        ])
    )

    with pytest.raises(ProverToolError, match="bound"):
        collect_releases()


def test_a_release_without_a_tag_is_refused(cmd_mox: CmdMox) -> None:
    """A tagless release cannot inform revision allocation."""
    cmd_mox.stub("gh").runs(_serve([{"tagName": "", "targetCommitish": ""}]))

    with pytest.raises(ProverToolError, match="tag"):
        collect_releases()


def test_a_failed_call_is_refused(cmd_mox: CmdMox) -> None:
    """An unread list is not evidence that no release exists."""

    def fail(invocation: Invocation) -> tuple[str, str, int]:
        """Answer with a server failure."""
        return ("", "gh: HTTP 500", 1)

    cmd_mox.stub("gh").runs(fail)

    with pytest.raises(ProverToolError, match="gh failed"):
        collect_releases()


def test_the_command_writes_the_release_document(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The resolve job runs a command, not an imported function."""
    cmd_mox.stub("gh").runs(
        _serve([{"tagName": fixtures.TAG, "targetCommitish": fixtures.BUILDER_COMMIT}])
    )
    output = tmp_path / "releases.json"

    assert main(["--output", str(output)]) == 0

    assert parse_publications(output.read_bytes())[0].tag == fixtures.TAG


def test_collected_releases_drive_identity_allocation(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The collector's document is exactly what `identity` accepts."""
    cmd_mox.stub("gh").runs(
        _serve([{"tagName": fixtures.TAG, "targetCommitish": fixtures.BUILDER_COMMIT}])
    )
    releases = tmp_path / "releases.json"
    assert main(["--output", str(releases)]) == 0
    resolution = tmp_path / "resolution.json"
    resolution.write_bytes(resolution_to_json(fixtures.resolution()))

    assert (
        entrypoint([
            "identity",
            "--resolution",
            str(resolution),
            "--builder-commit",
            fixtures.BUILDER_COMMIT,
            "--month",
            "2026-09",
            "--releases",
            str(releases),
            "--output",
            str(tmp_path / "identity.json"),
            "--run-id",
            str(fixtures.workflow().run_id),
            "--run-attempt",
            str(fixtures.workflow().run_attempt),
            "--event",
            fixtures.workflow().event,
        ])
        == 0
    )
