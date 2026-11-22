"""Publication drives the release through the real adapter.

`FakeGitHub` is the `gh` double the producer is published against. It stores
uploaded bytes and serves them again on download, so the digest that releases
publication is computed from what the release would really serve, and it models
GitHub's Git side: creating a draft creates no tag, so the tag ref stays absent
until the draft is published, and the release-by-tag endpoint cannot see a
draft, which forces the producer onto the release-list fallback. The tests here
run the whole guarded producer sequence and then publish through that double.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import typing as typ

import pytest

from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.models import REPOSITORY, SUPPORTED_TARGET
from rust_prover_tools.kani_snapshot.orchestrator.entrypoint import main
from rust_prover_tools.kani_snapshot.orchestrator.release_evidence import EVIDENCE_NAMES
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    parse_build_plan,
    parse_resolution,
)
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    parse_publication_identity,
)
from rust_prover_tools.kani_snapshot.producer.validation_receipt import (
    parse_receipt,
    verify_receipt,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures
from rust_prover_tools.unittests.test_kani_snapshot_producer_entrypoint import (
    MANUAL_EVENT,
    _prepare,
    _run_flags,
)

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


class FakeGitHub:
    """A minimal release server that `gh` is stubbed to answer with.

    It stores uploaded bytes and serves them again on download, so the digest
    that releases publication is computed from what the release would really
    serve. Every other call shape raises, which catches an adapter drift.
    """

    def __init__(self) -> None:
        """Create an absent release."""
        self.exists = False
        self.is_draft = False
        self.assets: dict[str, bytes] = {}
        self.target = ""
        self.body: str | None = None
        self.published = False

    def document(self) -> str:
        """Return the release as `gh api --jq` would print it."""
        return json.dumps({
            "id": 4242,
            "tag_name": fixtures.TAG,
            "draft": self.is_draft,
            "target_commitish": self.target,
            "body": self.body,
            "assets": [
                {
                    "name": name,
                    "size": len(data),
                    "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
                }
                for name, data in sorted(self.assets.items())
            ],
        })

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh` invocation as the release API would."""
        args = invocation.args
        if args[0] == "attestation":
            return self._attestation(args)
        if args[0] == "api":
            return self._api(args)
        if args[0] == "release":
            return self._release(args)
        msg = f"unexpected gh command: {args}"
        raise AssertionError(msg)

    def _attestation(self, args: list[str]) -> tuple[str, str, int]:
        """Answer `gh attestation verify` for a bundle this run signed.

        Publication re-verifies a published release under the consumer's
        policy, so the double refuses a command that does not ask for the
        pinned signer and builder commit. A matching answer names this run and
        attempt, which is what the manifest records.
        """
        assert args[1] == "verify", "attestation must run the verify subcommand"
        assert args[args.index("--signer-workflow") + 1] == (
            f"{REPOSITORY}/.github/workflows/kani-monthly.yml"
        ), "attestation must name the pinned signer workflow"
        assert args[args.index("--source-digest") + 1] == fixtures.BUILDER_COMMIT, (
            "attestation must bind the builder commit as its source digest"
        )
        workflow = fixtures.workflow(event=MANUAL_EVENT)
        invocation = (
            f"https://github.com/{REPOSITORY}"
            f"/actions/runs/{workflow.run_id}/attempts/{workflow.run_attempt}"
        )
        return (
            json.dumps([
                {
                    "verificationResult": {
                        "statement": {
                            "predicate": {
                                "runDetails": {"metadata": {"invocationId": invocation}}
                            }
                        }
                    }
                }
            ]),
            "",
            0,
        )

    def _api(self, args: list[str]) -> tuple[str, str, int]:
        """Answer one REST call."""
        endpoint = args[1]
        if endpoint == f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}":
            return self._read_by_tag()
        if endpoint == f"repos/{REPOSITORY}/git/ref/tags/{fixtures.TAG}":
            return self._read_tag_ref()
        if endpoint.startswith(f"repos/{REPOSITORY}/releases?"):
            listed = [json.loads(self.document())] if self.exists else []
            return (json.dumps(listed), "", 0)
        if args[1:3] == ["--method", "POST"]:
            return self._create(args)
        if args[1:3] == ["--method", "PATCH"]:
            return self._publish()
        msg = f"unexpected gh api call: {args}"
        raise AssertionError(msg)

    def _read_by_tag(self) -> tuple[str, str, int]:
        """Answer the release-by-tag endpoint, which cannot see a draft.

        GitHub's release-by-tag endpoint reports only published releases, so a
        draft answers not-found and the producer must find it in the release
        list instead.
        """
        if not self.exists or self.is_draft:
            return ("", "gh: HTTP 404: Not Found", 1)
        return (self.document(), "", 0)

    def _read_tag_ref(self) -> tuple[str, str, int]:
        """Answer the tag-ref lookup with the commit the tag addresses.

        Creating a draft creates no tag: publishing the draft is what creates
        it, so the tag ref is absent until `_publish` has succeeded.
        """
        if not self.published:
            return ("", "gh: HTTP 404: Not Found", 1)
        return (json.dumps({"type": "commit", "sha": self.target}), "", 0)

    def _create(self, args: list[str]) -> tuple[str, str, int]:
        """Answer a draft-creation call, recording what the draft declares."""
        self.exists = True
        self.is_draft = True
        self.target = next(
            value.removeprefix("target_commitish=")
            for value in args
            if value.startswith("target_commitish=")
        )
        self.body = next(
            value.removeprefix("body=") for value in args if value.startswith("body=")
        )
        return ("{}", "", 0)

    def _publish(self) -> tuple[str, str, int]:
        """Answer a publication call, refusing an incomplete draft."""
        assert set(self.assets) == _release_names(), (
            "publish must find exactly the release's asset names"
        )
        self.is_draft = False
        self.published = True
        return ("{}", "", 0)

    def _release(self, args: list[str]) -> tuple[str, str, int]:
        """Answer one release upload or download."""
        if args[1] == "upload":
            path = pathlib.Path(args[3])
            self.assets[path.name] = path.read_bytes()
            return ("", "", 0)
        if args[1] == "download":
            name = args[args.index("--pattern") + 1]
            directory = pathlib.Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
            return ("", "", 0)
        msg = f"unexpected gh release call: {args}"
        raise AssertionError(msg)


def _release_names() -> set[str]:
    """Return every asset name the release must serve."""
    return set(fixtures.names().values()) | set(EVIDENCE_NAMES)


def test_the_entrypoint_runs_the_whole_producer_sequence(
    tmp_path: pathlib.Path,
) -> None:
    """Each job's output parses and binds to the run that produced it."""
    _prepare(tmp_path)
    resolution = parse_resolution((tmp_path / "resolution.json").read_bytes())
    identity = parse_publication_identity((tmp_path / "identity.json").read_bytes())
    plan = parse_build_plan((tmp_path / "plan.json").read_bytes())
    parsed = parse_manifest((tmp_path / "release" / "manifest.json").read_bytes())
    receipt = parse_receipt((tmp_path / "receipt.json").read_bytes())
    assert resolution.route.value == "manual", "route must be manual"
    assert identity.tag == fixtures.TAG, "identity must adopt the pinned tag"
    assert identity.upstream_commit == resolution.upstream_commit, (
        "identity must reuse the resolved commit"
    )
    assert plan.upstream_commit == resolution.upstream_commit, (
        "plan must build the resolved commit"
    )
    assert plan.tag == fixtures.TAG, "plan must carry the pinned tag"
    assert parsed.manifest.tag == plan.tag, "manifest tag must match the plan"
    assert parsed.manifest.builder_commit == fixtures.BUILDER_COMMIT, (
        "manifest must record the builder commit"
    )
    assert parsed.manifest.workflow == fixtures.workflow(event=MANUAL_EVENT), (
        "manifest must record the dispatching run"
    )
    assert parsed.manifest.target == SUPPORTED_TARGET, (
        "manifest must record the supported target"
    )
    verify_receipt(receipt, parsed)


def test_the_entrypoint_writes_checksums_beside_the_manifest(
    tmp_path: pathlib.Path,
) -> None:
    """`SHA256SUMS` covers the payloads and the manifest bytes it names."""
    _prepare(tmp_path)
    manifest = (tmp_path / "release" / "manifest.json").read_bytes()
    sums = (tmp_path / "release" / "SHA256SUMS").read_text(encoding="ascii")
    # Each line is `<digest>  <name>`, so the split names the digest per file.
    rows = {
        name: digest
        for digest, name in (line.split("  ", 1) for line in sums.splitlines())
    }
    assert rows["manifest.json"] == hashlib.sha256(manifest).hexdigest(), (
        "SHA256SUMS must cover the manifest bytes"
    )
    for name in fixtures.names().values():
        assert name in rows, f"SHA256SUMS must cover {name}"


def test_publish_refuses_a_receipt_from_another_run_before_any_call(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cross-run receipt is refused before the release API is touched.

    The command line names the same run the manifest does; only the receipt
    was written by another run. Changing the command's flags as well would let
    a publisher pass this test by comparing the flags with the manifest alone,
    never reading the receipt's own run binding — so the receipt is the single
    thing that differs here.
    """
    _prepare(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["run_id"] = 999
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    calls: list[list[str]] = []

    def record(invocation: Invocation) -> tuple[str, str, int]:
        """Record any accidental release call and answer nothing useful."""
        calls.append(invocation.args)
        return ("", "", 0)

    cmd_mox.stub("gh").runs(record)
    code = main([
        "publish",
        "--manifest",
        str(tmp_path / "release" / "manifest.json"),
        "--receipt",
        str(receipt_path),
        "--payloads",
        str(tmp_path / "release"),
        *_run_flags(),
    ])
    assert code == 1, "publish must refuse a receipt from another run"
    assert "another workflow run or attempt" in capsys.readouterr().err, (
        "the refusal must name the run binding"
    )
    assert not calls, "a refused receipt must not touch the release API"


# This drives the real adapter, so every `gh` call is a fresh shim process:
# about a dozen interpreter start-ups that the 30 s default cannot absorb when
# the host is building. The budget covers start-up, not a slower assertion.
@pytest.mark.timeout(120)
def test_publish_drives_the_release_through_the_real_adapter(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole flow publishes the exact measured bytes and reports success."""
    _prepare(tmp_path)
    github = FakeGitHub()
    cmd_mox.stub("gh").runs(github.handle)
    code = main([
        "publish",
        "--manifest",
        str(tmp_path / "release" / "manifest.json"),
        "--receipt",
        str(tmp_path / "receipt.json"),
        "--payloads",
        str(tmp_path / "release"),
        *_run_flags(),
    ])
    assert code == 0, "publish must succeed for a complete, verified release"
    assert github.published, "the release must be published"
    assert not github.is_draft, "the release must no longer be a draft"
    assert github.target == fixtures.BUILDER_COMMIT, (
        "the draft must target the builder commit"
    )
    expected = {
        path.name: path.read_bytes() for path in (tmp_path / "release").iterdir()
    }
    assert github.assets == expected, (
        "the release must serve exactly the assembled bytes"
    )
    assert fixtures.TAG in capsys.readouterr().out, (
        "publish must report the published tag"
    )
