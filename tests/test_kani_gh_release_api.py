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
from rust_prover_tools.kani_snapshot.gh_release.api import GhReleaseApi
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_policy import ReleaseTransientError
from tests import kani_publish_fixtures as fixtures
from tests.test_kani_gh_release_fixtures import _ReleaseServer

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation


pytest_plugins = ("cmd_mox.pytest_plugin",)

#: A commit the immutable tag addresses, distinct from release metadata.
TAG_COMMIT: typ.Final[str] = "a" * 40
#: A second tag object, so an annotated tag can be shown to peel.
TAG_OBJECT: typ.Final[str] = "9" * 40


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


def test_discard_deletes_the_placeholder_by_its_server_identifier(
    cmd_mox: CmdMox,
) -> None:
    """Replacing a failed upload deletes the record the observation reported."""
    cmd_mox.mock("gh").with_args(
        "api",
        "--method",
        "DELETE",
        f"repos/{REPOSITORY}/releases/assets/4242",
    ).returns(stdout="")
    GhReleaseApi().discard(
        fixtures.TAG, fixtures.placeholder("backend.tar.gz", asset_id=4242)
    )


def test_discard_refuses_a_record_without_an_identifier() -> None:
    """A record copied from another source cannot be deleted by name alone."""
    with pytest.raises(ProverToolError, match="no server identifier"):
        GhReleaseApi().discard(
            fixtures.TAG, fixtures.placeholder("backend.tar.gz", asset_id=0)
        )


def test_observe_keeps_the_identifier_a_discard_needs(cmd_mox: CmdMox) -> None:
    """An empty record is reported as a placeholder together with its id."""
    server = _ReleaseServer(
        release=fixtures.release_json(
            draft=True,
            assets=[
                {
                    "id": 4242,
                    "name": "backend.tar.gz",
                    "size": 0,
                    "digest": None,
                }
            ],
        ),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": (
                "commit",
                TAG_COMMIT,
            )
        },
    )
    cmd_mox.stub("gh").runs(server.handle)

    (asset,) = GhReleaseApi().observe(fixtures.TAG).assets

    assert asset.is_placeholder, "an empty record was not recognised as a placeholder"
    assert asset.asset_id == 4242, "the identifier a delete needs was dropped"


def test_observe_parses_a_draft_without_assets(cmd_mox: CmdMox) -> None:
    """An empty draft is an observation, not an error."""
    server = _ReleaseServer(
        release=fixtures.release_json(draft=True),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": (
                "commit",
                TAG_COMMIT,
            )
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert observed.exists, "an empty draft was reported as absent"
    assert observed.is_draft, "an empty draft was reported as published"
    assert not observed.assets, "an empty draft reported assets"
    assert observed.release_id == 4242, "the draft's identifier was dropped"
    assert observed.target_commitish == TAG_COMMIT, (
        "an empty draft did not report the tag's commit"
    )


def test_observe_peels_an_annotated_tag_to_its_commit(cmd_mox: CmdMox) -> None:
    """An annotated tag decides the commit, and the tag object is peeled."""
    server = _ReleaseServer(
        release=fixtures.release_json(draft=True),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": ("tag", TAG_OBJECT),
            f"repos/{REPOSITORY}/git/tags/{TAG_OBJECT}": ("commit", TAG_COMMIT),
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    assert GhReleaseApi().observe(fixtures.TAG).target_commitish == TAG_COMMIT, (
        "the annotated tag was not peeled to its commit"
    )


def test_observe_accepts_four_annotated_tag_objects_before_the_commit(
    cmd_mox: CmdMox,
) -> None:
    """Four peels are within budget; the commit is checked after the last one."""
    first, second, third, fourth = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)
    server = _ReleaseServer(
        release=fixtures.release_json(draft=True),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": ("tag", first),
            f"repos/{REPOSITORY}/git/tags/{first}": ("tag", second),
            f"repos/{REPOSITORY}/git/tags/{second}": ("tag", third),
            f"repos/{REPOSITORY}/git/tags/{third}": ("tag", fourth),
            f"repos/{REPOSITORY}/git/tags/{fourth}": ("commit", TAG_COMMIT),
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    assert GhReleaseApi().observe(fixtures.TAG).target_commitish == TAG_COMMIT, (
        "four peels did not reach the commit before the budget was spent"
    )


def test_observe_refuses_a_fifth_annotated_tag_object(cmd_mox: CmdMox) -> None:
    """A chain longer than the peel budget names no commit to trust."""
    first, second, third, fourth, fifth = (
        "1" * 40,
        "2" * 40,
        "3" * 40,
        "4" * 40,
        "5" * 40,
    )
    server = _ReleaseServer(
        release=fixtures.release_json(draft=True),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": ("tag", first),
            f"repos/{REPOSITORY}/git/tags/{first}": ("tag", second),
            f"repos/{REPOSITORY}/git/tags/{second}": ("tag", third),
            f"repos/{REPOSITORY}/git/tags/{third}": ("tag", fourth),
            f"repos/{REPOSITORY}/git/tags/{fourth}": ("tag", fifth),
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    with pytest.raises(ProverToolError, match="chains through more than"):
        GhReleaseApi().observe(fixtures.TAG)
    assert not any(fifth in arg for call in server.calls for arg in call), (
        "the fifth tag object must not be fetched"
    )


def test_observe_reports_a_tag_that_no_release_carries(cmd_mox: CmdMox) -> None:
    """An orphan tag is reported as a fact; policy decides it cannot be adopted."""
    server = _ReleaseServer(
        release=None,
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": (
                "commit",
                TAG_COMMIT,
            )
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert not observed.exists, "an orphan tag was reported as a release"
    assert observed.tag_commit == TAG_COMMIT, "the orphan tag's commit was dropped"
    assert server.calls[-1][1] == f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}", (
        "the tag ref was not the last read before the orphan was reported"
    )


def test_observe_reports_a_missing_release(cmd_mox: CmdMox) -> None:
    """The tag endpoint hides drafts, so an absent tag is confirmed by list."""
    server = _ReleaseServer(release=None)
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert not observed.exists, "an absent release was reported as existing"
    assert [args[1] for args in server.calls] == [
        f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}",
        f"repos/{REPOSITORY}/releases?per_page=100&page=1",
        f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}",
    ], "the list endpoint was not consulted for the draft-visible answer"


def test_observe_finds_a_draft_through_the_release_list(cmd_mox: CmdMox) -> None:
    """A draft the tag endpoint cannot see is still observed, not absent."""
    server = _ReleaseServer(
        release=None,
        listed=f"[{fixtures.release_json(draft=True)}]",
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": (
                "commit",
                TAG_COMMIT,
            )
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert observed.exists, "a draft found through the list was reported as absent"
    assert observed.is_draft, "a draft found through the list was reported as published"
    assert observed.release_id == 4242, "the listed draft's identifier was dropped"
    assert observed.target_commitish == TAG_COMMIT, (
        "the listed draft did not report the tag's commit"
    )


def test_observe_reports_a_published_release_without_a_tag(cmd_mox: CmdMox) -> None:
    """A published release with no resolvable tag is reported, not judged."""
    server = _ReleaseServer(release=fixtures.release_json(draft=False))
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert not observed.is_draft, "the release under test was a draft"
    assert observed.tag_commit is None, (
        "a tag that does not exist must be reported as unresolved"
    )


def test_observe_keeps_a_draft_target_when_the_tag_is_absent(
    cmd_mox: CmdMox,
) -> None:
    """A draft has no tag yet, so its recorded target is the only answer."""
    server = _ReleaseServer(release=fixtures.release_json(draft=True))
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert observed.is_draft, "a draft was reported as published"
    assert observed.target_commitish == fixtures.BUILDER, (
        "a missing tag leaves target_commitish unchanged at the draft's target"
    )


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


def test_verify_downloads_every_served_asset_and_hashes_it(
    cmd_mox: CmdMox,
) -> None:
    """The digest that authorises a publish is computed from served bytes."""
    downloaded: list[str] = []

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Write the requested asset into the download directory."""
        args = invocation.args
        name = args[args.index("--pattern") + 1]
        downloaded.append(name)
        directory = pathlib.Path(args[args.index("--dir") + 1])
        (directory / name).write_bytes(fixtures.PAYLOADS[name])
        return ("", "", 0)

    cmd_mox.stub("gh").runs(handler)
    observed = fixtures.draft(fixtures.verified_assets(), provenance_verified=False)
    verified = GhReleaseApi().verify(fixtures.TAG, observed, ["backend.tar.gz"])
    assert set(downloaded) == set(fixtures.PAYLOADS), (
        "verification must download the whole release, not only the named asset"
    )
    assert verified.digest_map() == {
        name: fixtures.digest(data) for name, data in fixtures.PAYLOADS.items()
    }, "verification did not hash the exact bytes the release served"
    assert not verified.provenance_verified, "payloads alone are not evidence"
    assert verified.reason is not None, "a failed provenance check gave no reason"
    assert "evidence assets" in verified.reason, (
        "the failure did not name the missing evidence assets"
    )


def test_verify_refuses_names_the_release_does_not_report() -> None:
    """A caller cannot verify bytes the observation never saw."""
    observed = fixtures.draft(fixtures.verified_assets())
    with pytest.raises(ProverToolError, match="does not report"):
        GhReleaseApi().verify(fixtures.TAG, observed, ["absent.tar.gz"])
    with pytest.raises(ProverToolError, match="at least one asset"):
        GhReleaseApi().verify(fixtures.TAG, observed, ())


def test_observe_is_a_query_that_downloads_nothing(cmd_mox: CmdMox) -> None:
    """A state query reports metadata and never claims a verification."""
    server = _ReleaseServer(
        release=fixtures.release_json(
            draft=True,
            assets=[
                {"name": name, "size": len(data), "digest": None}
                for name, data in fixtures.PAYLOADS.items()
            ],
        ),
        objects={
            f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}": (
                "commit",
                TAG_COMMIT,
            )
        },
    )
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert [call[0] for call in server.calls] == ["api", "api"], (
        "the observation transferred bytes"
    )
    assert not observed.provenance_verified, (
        "a metadata query claimed to authenticate the release"
    )
    assert all(asset.sha256 is None for asset in observed.assets), (
        "a metadata query reported digests it did not compute"
    )


def test_the_adapter_refuses_a_foreign_repository() -> None:
    """The adapter addresses one repository and never reads it from input."""
    with pytest.raises(ProverToolError, match="may only address"):
        GhReleaseApi("attacker/rust-prover-tools")
