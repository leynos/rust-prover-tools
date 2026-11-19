"""The `gh` adapter is checked by argument vector, not by prose.

Publication safety depends on the adapter doing exactly what the policy assumes:
a draft is created as a prerelease at the recorded builder commit, publishing is
a PATCH of that draft that never marks it latest, uploads never clobber, a 404 is
the absent state while a 5xx is transient, and the digest that releases a
publication is computed from the bytes the release actually serves.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_api import GhReleaseApi
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_policy import ReleaseTransientError
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


def test_create_draft_builds_a_draft_prerelease_at_the_builder_commit(
    cmd_mox: CmdMox,
) -> None:
    """The draft is immutable, a prerelease and targeted at the recorded SHA."""
    cmd_mox.mock("gh").with_args(
        "api",
        "--method",
        "POST",
        f"repos/{REPOSITORY}/releases",
        "-f",
        f"tag_name={fixtures.TAG}",
        "-f",
        f"target_commitish={fixtures.BUILDER}",
        "-f",
        f"name={fixtures.TAG}",
        "-f",
        f"body={fixtures.body()}",
        "-F",
        "draft=true",
        "-F",
        "prerelease=true",
    ).returns(stdout="{}")
    GhReleaseApi().create_draft(fixtures.expected())


def test_publish_makes_the_release_public_without_marking_it_latest(
    cmd_mox: CmdMox,
) -> None:
    """Publication is a PATCH of the draft, never a create or a replace."""
    cmd_mox.mock("gh").with_args(
        "api",
        "--method",
        "PATCH",
        f"repos/{REPOSITORY}/releases/4242",
        "-F",
        "draft=false",
        "-f",
        "make_latest=false",
    ).returns(stdout="{}")
    GhReleaseApi().publish(fixtures.TAG, 4242)


def test_upload_uses_release_upload_without_clobber(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The upload command names the asset and repository exactly."""
    name = "backend.tar.gz"
    path = tmp_path / name
    path.write_bytes(fixtures.PAYLOADS[name])
    cmd_mox.mock("gh").with_args(
        "release", "upload", fixtures.TAG, str(path), "--repo", REPOSITORY
    ).returns(stdout="")
    GhReleaseApi().upload(fixtures.TAG, fixtures.asset(name), path)


def test_observe_parses_a_draft_without_assets(cmd_mox: CmdMox) -> None:
    """An empty draft is an observation, not an error."""
    cmd_mox.mock("gh").with_args(
        "api",
        f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}",
        "--jq",
        fixtures.OBSERVE_QUERY,
    ).returns(stdout=fixtures.release_json(draft=True))
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert observed.exists
    assert observed.is_draft
    assert not observed.assets
    assert observed.release_id == 4242


def test_observe_reports_a_missing_release(cmd_mox: CmdMox) -> None:
    """The tag endpoint hides drafts, so an absent tag is confirmed by list."""
    calls: list[str] = []

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Answer the tag endpoint with 404 and the list with no releases."""
        path = invocation.args[1]
        calls.append(path)
        if "/releases/tags/" in path:
            return ("", "gh: HTTP 404: Not Found", 1)
        return ("[]", "", 0)

    cmd_mox.stub("gh").runs(handler)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert not observed.exists
    assert calls == [
        f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}",
        f"repos/{REPOSITORY}/releases?per_page=100&page=1",
    ], "the list endpoint was not consulted for the draft-visible answer"


def test_observe_classifies_a_server_error_as_transient(cmd_mox: CmdMox) -> None:
    """A retryable server error must not be mistaken for absence."""
    cmd_mox.mock("gh").with_args(
        "api",
        f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}",
        "--jq",
        fixtures.OBSERVE_QUERY,
    ).returns(exit_code=1, stderr="gh: HTTP 502: Bad Gateway")
    with pytest.raises(ReleaseTransientError):
        GhReleaseApi().observe(fixtures.TAG)


def test_verify_assets_hashes_the_downloaded_bytes(cmd_mox: CmdMox) -> None:
    """The digest that authorises a publish is computed from served bytes."""

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Write the requested asset into the download directory."""
        args = invocation.args
        name = args[args.index("--pattern") + 1]
        directory = pathlib.Path(args[args.index("--dir") + 1])
        (directory / name).write_bytes(fixtures.PAYLOADS[name])
        return ("", "", 0)

    cmd_mox.stub("gh").runs(handler)
    digests = GhReleaseApi().verify_assets(fixtures.TAG, ["backend.tar.gz"])
    assert digests == {
        "backend.tar.gz": fixtures.digest(fixtures.PAYLOADS["backend.tar.gz"])
    }


def test_the_adapter_refuses_a_foreign_repository() -> None:
    """The adapter addresses one repository and never reads it from input."""
    with pytest.raises(ProverToolError, match="may only address"):
        GhReleaseApi("attacker/rust-prover-tools")
