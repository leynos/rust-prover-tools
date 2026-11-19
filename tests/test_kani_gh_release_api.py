"""The `gh` adapter is checked by argument vector, not by prose.

Publication safety depends on the adapter doing exactly what the policy assumes:
a draft is created as a prerelease at the recorded builder commit, publishing is
a PATCH of that draft that never marks it latest, uploads never clobber, a 404 is
the absent state while a 5xx is transient, and the digest that releases a
publication is computed from the bytes the release actually serves.
"""

from __future__ import annotations

import json
import pathlib
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_api import GhReleaseApi
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.producer.assemble import MANIFEST_NAME
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.release_assets import (
    ATTESTATION_NAME,
    CHECKSUMS_NAME,
)
from rust_prover_tools.kani_snapshot.release_model import release_body
from rust_prover_tools.kani_snapshot.release_policy import ReleaseTransientError
from rust_prover_tools.unittests import kani_producer_fixtures as producer
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation


pytest_plugins = ("cmd_mox.pytest_plugin",)

#: A commit the immutable tag addresses, distinct from release metadata.
TAG_COMMIT: typ.Final[str] = "a" * 40
#: A second tag object, so an annotated tag can be shown to peel.
TAG_OBJECT: typ.Final[str] = "9" * 40


def _object(kind: str, sha: str) -> str:
    """Return a Git object reference as `--jq .object` prints it."""
    return json.dumps({"type": kind, "sha": sha})


class _ReleaseServer:
    """Answer the release and tag-ref lookups one observation makes.

    `cmd_mox` keeps one double per command name, so a test that needs both a
    release document and a tag ref answers them from a single handler rather
    than registering competing mocks.
    """

    def __init__(
        self,
        *,
        release: str | None,
        objects: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        """Record the release document and any resolvable tag objects."""
        self._release = release
        self._objects = objects or {}
        self.calls: list[list[str]] = []

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh api` call, recording the argument vector."""
        args = list(invocation.args)
        self.calls.append(args)
        path = args[1]
        if path in self._objects:
            assert args[2:] == ["--jq", ".object"], args
            return (_object(*self._objects[path]), "", 0)
        if self._release is not None and "/releases/tags/" in path:
            assert args[2:] == ["--jq", fixtures.OBSERVE_QUERY], args
            return (self._release, "", 0)
        if path.startswith(f"repos/{REPOSITORY}/releases?"):
            return ("[]", "", 0)
        return ("", "gh: HTTP 404: Not Found", 1)


def _fingerprint() -> str:
    """Return the build-input fingerprint the assembled fixture records."""
    return input_fingerprint(
        upstream_commit=producer.UPSTREAM_COMMIT,
        builder_commit=producer.BUILDER_COMMIT,
        recipe=pinned_recipe(),
    )


class _ServedRelease:
    """A published release `gh` answers with, serving its exact bytes.

    Every read the adapter performs is answered from one directory, so the
    digests and the publication checks are computed from what the release
    really serves rather than from a claim in the document.
    """

    def __init__(self, directory: pathlib.Path, body_text: str | None) -> None:
        """Record the served directory and the release body."""
        self._directory = directory
        self._body = body_text
        self._names = sorted(path.name for path in directory.iterdir())

    def document(self) -> str:
        """Return the release as `gh api --jq` would print it."""
        return json.dumps({
            "id": 4242,
            "tag_name": producer.TAG,
            "draft": False,
            "target_commitish": producer.BUILDER_COMMIT,
            "body": self._body,
            "assets": [
                {
                    "name": name,
                    "size": (self._directory / name).stat().st_size,
                    "digest": f"sha256:{fixtures.digest(self._data(name))}",
                }
                for name in self._names
            ],
        })

    def _data(self, name: str) -> bytes:
        """Return one served file's exact bytes."""
        return (self._directory / name).read_bytes()

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh` invocation from the served release."""
        args = list(invocation.args)
        if args[:2] == ["api", f"repos/{REPOSITORY}/releases/tags/{producer.TAG}"]:
            return (self.document(), "", 0)
        if args[:2] == ["api", f"repos/{REPOSITORY}/git/ref/tags/{producer.TAG}"]:
            return (_object("commit", TAG_COMMIT), "", 0)
        if args[:2] == ["release", "download"]:
            name = args[args.index("--pattern") + 1]
            directory = pathlib.Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self._data(name))
            return ("", "", 0)
        msg = f"unexpected gh call: {args}"
        raise AssertionError(msg)


def _served_release(
    tmp_path: pathlib.Path,
    *,
    attestation: bytes | None = None,
    fingerprint: str | None = None,
) -> _ServedRelease:
    """Serve a real assembled release, optionally with altered provenance."""
    directory = tmp_path / "served"
    assembly = producer.assemble_into(directory)
    (directory / MANIFEST_NAME).write_bytes(assembly.manifest_bytes)
    (directory / CHECKSUMS_NAME).write_bytes(assembly.checksums)
    (directory / ATTESTATION_NAME).write_bytes(
        producer.attestation_bundle(assembly.manifest_sha256)
        if attestation is None
        else attestation
    )
    body_text = release_body(
        producer.TAG,
        fingerprint or _fingerprint(),
        run_id=fixtures.RUN_ID,
        run_attempt=fixtures.RUN_ATTEMPT,
    )
    return _ServedRelease(directory, body_text)


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
    assert observed.exists
    assert observed.is_draft
    assert not observed.assets
    assert observed.release_id == 4242
    assert observed.target_commitish == TAG_COMMIT


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
    assert GhReleaseApi().observe(fixtures.TAG).target_commitish == TAG_COMMIT


def test_observe_refuses_a_tag_that_no_release_carries(cmd_mox: CmdMox) -> None:
    """An orphan tag would be adopted silently by a new draft."""
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
    with pytest.raises(ProverToolError, match="no release carries it"):
        GhReleaseApi().observe(fixtures.TAG)
    assert server.calls[-1][1] == f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}"


def test_observe_reports_a_missing_release(cmd_mox: CmdMox) -> None:
    """The tag endpoint hides drafts, so an absent tag is confirmed by list."""
    server = _ReleaseServer(release=None)
    cmd_mox.stub("gh").runs(server.handle)
    observed = GhReleaseApi().observe(fixtures.TAG)
    assert not observed.exists
    assert [args[1] for args in server.calls] == [
        f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}",
        f"repos/{REPOSITORY}/releases?per_page=100&page=1",
        f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}",
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


def test_observe_verifies_a_published_release_from_its_own_bytes(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A release whose evidence, identity and attestation agree is verified."""
    cmd_mox.stub("gh").runs(_served_release(tmp_path).handle)
    observed = GhReleaseApi().observe(producer.TAG)
    assert observed.provenance_verified
    assert observed.target_commitish == TAG_COMMIT


@pytest.mark.parametrize(
    "attestation",
    [
        pytest.param(
            b'{"dsseEnvelope": {"payload": "", "signatures": []}}',
            id="empty-envelope",
        ),
        pytest.param(
            producer.attestation_bundle("e" * 64), id="statement-about-other-bytes"
        ),
    ],
)
def test_observe_reports_an_unrelated_attestation_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    attestation: bytes,
) -> None:
    """An attestation that does not name this manifest is not provenance."""
    cmd_mox.stub("gh").runs(_served_release(tmp_path, attestation=attestation).handle)
    assert not GhReleaseApi().observe(producer.TAG).provenance_verified


def test_observe_reports_a_body_that_does_not_bind_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A body recording other build inputs does not authenticate the release."""
    served = _served_release(tmp_path, fingerprint=fixtures.OTHER_FINGERPRINT)
    cmd_mox.stub("gh").runs(served.handle)
    assert not GhReleaseApi().observe(producer.TAG).provenance_verified


def test_the_adapter_refuses_a_foreign_repository() -> None:
    """The adapter addresses one repository and never reads it from input."""
    with pytest.raises(ProverToolError, match="may only address"):
        GhReleaseApi("attacker/rust-prover-tools")
