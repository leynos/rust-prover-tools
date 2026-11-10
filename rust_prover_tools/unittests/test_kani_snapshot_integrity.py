"""Tests for manifest, payload and attestation authentication."""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import typing as typ

import pytest

from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_snapshot.attestation import (
    AttestationPolicy,
    AttestationRequest,
    attestation_command,
    policy_for_pin,
    verify_attestation,
)
from rust_prover_tools.kani_snapshot.integrity import (
    verify_asset,
    verify_checksums,
    verify_manifest_digest,
)
from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.models import AssetRecord
from tests.kani_snapshot_fixtures import (
    BUILDER_COMMIT,
    make_manifest,
    make_pin,
    manifest_bytes,
    pin_bytes,
    with_manifest,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


def _invocation(policy: AttestationPolicy, run_id: int, attempt: int) -> str:
    """Return the GitHub invocation identifier of one workflow attempt."""
    return (
        f"https://github.com/{policy.repository}"
        f"/actions/runs/{run_id}/attempts/{attempt}"
    )


def _verified(invocation_id: str) -> str:
    """Return the verifier output for one verified record naming a run."""
    return json.dumps([
        {
            "attestation": {},
            "verificationResult": {
                "signature": {"certificate": {}},
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {
                        "runDetails": {"metadata": {"invocationId": invocation_id}}
                    },
                },
            },
        }
    ])


def _request(directory: Path) -> AttestationRequest:
    """Return one attestation request naming the fixture manifest's run."""
    return AttestationRequest(
        policy_for_pin(make_pin()),
        directory / "m.json",
        directory / "b.jsonl",
        make_manifest().workflow,
    )


def _matching_output() -> str:
    """Return verifier output naming the fixture manifest's own run."""
    policy = policy_for_pin(make_pin())
    workflow = make_manifest().workflow
    return _verified(_invocation(policy, workflow.run_id, workflow.run_attempt))


def _write_asset(path: Path, data: bytes) -> AssetRecord:
    """Write `data` and return its matching asset record."""
    path.write_bytes(data)
    return AssetRecord(
        "backend",
        path.name,
        "application/gzip",
        len(data),
        hashlib.sha256(data).hexdigest(),
    )


def test_manifest_digest_must_match_the_pin() -> None:
    """A matching digest passes; a different one is refused."""
    manifest = make_manifest()
    parsed = parse_manifest(manifest_bytes(manifest))
    verify_manifest_digest(parsed, with_manifest(make_pin(), manifest))
    with pytest.raises(ProverToolError, match="does not match the reviewed pin"):
        verify_manifest_digest(parsed, make_pin())


def test_asset_size_and_digest_are_both_checked(tmp_path: Path) -> None:
    """A payload must match its recorded size and digest."""
    asset = _write_asset(tmp_path / "asset.bin", b"payload")
    verify_asset(tmp_path / "asset.bin", asset)
    with pytest.raises(ProverToolError, match=r"has \d+ bytes"):
        verify_asset(tmp_path / "asset.bin", dc.replace(asset, size=asset.size + 1))
    with pytest.raises(ProverToolError, match="digest does not match"):
        verify_asset(
            tmp_path / "asset.bin",
            AssetRecord(
                asset.role,
                asset.name,
                asset.media_type,
                asset.size,
                "0" * 64,
            ),
        )


def test_missing_and_non_regular_assets_are_refused(tmp_path: Path) -> None:
    """A missing path and a directory are not payloads."""
    asset = AssetRecord("backend", "asset.bin", "application/gzip", 1, "1" * 64)
    with pytest.raises(ProverToolError, match="failed to inspect"):
        verify_asset(tmp_path / "absent.bin", asset)
    (tmp_path / "dir").mkdir()
    with pytest.raises(ProverToolError, match="not a regular file"):
        verify_asset(tmp_path / "dir", asset)


def _checksum_text(manifest_sha256: str) -> str:
    """Return a matching checksum document for the synthetic manifest."""
    manifest = make_manifest()
    rows = [f"{asset.sha256}  {asset.name}" for asset in manifest.assets]
    rows.append(f"{manifest_sha256}  manifest.json")
    return "\n".join(rows) + "\n"


def test_checksums_must_agree_with_the_manifest(tmp_path: Path) -> None:
    """A complete, agreeing checksum document passes."""
    manifest = make_manifest()
    path = tmp_path / "SHA256SUMS"
    path.write_text(_checksum_text("c" * 64), encoding="utf-8")
    verify_checksums(path, manifest, "c" * 64)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda text: "\n".join(text.splitlines()[:-1]) + "\n",
            "missing 'manifest.json'",
        ),
        (
            lambda text: text.replace(
                "1" * 64 + "  backend.tar.gz", "0" * 64 + "  backend.tar.gz"
            ),
            "does not match the manifest",
        ),
        (lambda text: text + "not-a-row\n", "not '<digest> <name>'"),
        (
            lambda text: text + "1" * 64 + "  backend.tar.gz\n",
            "more than once",
        ),
        (
            lambda text: text.replace("backend.tar.gz", "../backend.tar.gz"),
            "unsafe asset name",
        ),
    ],
)
def test_bad_checksum_documents_are_refused(
    tmp_path: Path, mutate: cabc.Callable[[str], str], match: str
) -> None:
    """Missing, disagreeing, malformed, duplicate and unsafe rows fail."""
    path = tmp_path / "SHA256SUMS"
    path.write_text(mutate(_checksum_text("c" * 64)), encoding="utf-8")
    with pytest.raises(ProverToolError, match=match):
        verify_checksums(path, make_manifest(), "c" * 64)


def test_unreadable_checksums_are_reported(tmp_path: Path) -> None:
    """A missing checksum file names the path."""
    with pytest.raises(ProverToolError, match="failed to read snapshot checksums"):
        verify_checksums(tmp_path / "absent", make_manifest(), "c" * 64)


def test_attestation_policy_binds_pin_identity() -> None:
    """The policy names the canonical repository, workflow and builder SHA."""
    policy = policy_for_pin(make_pin())
    assert policy.repository == "leynos/rust-prover-tools"
    assert policy.signer_workflow.endswith(
        "leynos/rust-prover-tools/.github/workflows/kani-monthly.yml"
    )
    assert policy.source_digest == BUILDER_COMMIT
    assert policy.signer_digest == BUILDER_COMMIT
    assert policy.source_ref == "refs/heads/main"


def test_attestation_command_is_explicit(tmp_path: Path) -> None:
    """Every policy decision appears as an explicit verifier flag."""
    policy = policy_for_pin(make_pin())
    argv = attestation_command(policy, tmp_path / "manifest.json", tmp_path / "b.jsonl")
    assert argv[:4] == ("gh", "attestation", "verify", str(tmp_path / "manifest.json"))
    joined = " ".join(argv)
    for expected in (
        "--bundle",
        "--repo leynos/rust-prover-tools",
        "--signer-workflow",
        "--cert-oidc-issuer https://token.actions.githubusercontent.com",
        "--source-ref refs/heads/main",
        f"--source-digest {BUILDER_COMMIT}",
        f"--signer-digest {BUILDER_COMMIT}",
        "--predicate-type https://slsa.dev/provenance/v1",
        "--deny-self-hosted-runners",
        "--format json",
    ):
        assert expected in joined


def test_attestation_command_can_use_a_separate_trust_root(tmp_path: Path) -> None:
    """An explicitly provisioned trust root becomes a verifier flag."""
    policy = AttestationPolicy(
        repository="leynos/rust-prover-tools",
        signer_workflow="leynos/rust-prover-tools/.github/workflows/kani-monthly.yml",
        source_digest=BUILDER_COMMIT,
        signer_digest=BUILDER_COMMIT,
        trust_root=tmp_path / "trusted_root.jsonl",
    )
    argv = attestation_command(policy, tmp_path / "m.json", tmp_path / "b.jsonl")
    assert "--custom-trusted-root" in argv
    assert str(tmp_path / "trusted_root.jsonl") in argv


def test_verified_attestation_returns_records(tmp_path: Path, cmd_mox: CmdMox) -> None:
    """A non-empty verified JSON array is accepted."""

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Answer the verification with one verified record."""
        assert "--bundle" in invocation.args
        return (_matching_output(), "", 0)

    cmd_mox.stub("gh").runs(handler)
    records = verify_attestation(_request(tmp_path))
    assert len(records) == 1


def test_an_attestation_for_another_run_is_refused(
    tmp_path: Path, cmd_mox: CmdMox
) -> None:
    """A verified statement naming a different run cannot authenticate."""
    policy = policy_for_pin(make_pin())
    workflow = make_manifest().workflow
    other = _verified(_invocation(policy, workflow.run_id + 1, workflow.run_attempt))
    cmd_mox.stub("gh").runs(lambda invocation: (other, "", 0))
    with pytest.raises(ProverToolError, match="workflow run"):
        verify_attestation(_request(tmp_path))


def test_an_attestation_for_another_attempt_is_refused(
    tmp_path: Path, cmd_mox: CmdMox
) -> None:
    """A rerun's attestation does not stand in for the recorded attempt."""
    policy = policy_for_pin(make_pin())
    workflow = make_manifest().workflow
    rerun = _verified(_invocation(policy, workflow.run_id, workflow.run_attempt + 1))
    cmd_mox.stub("gh").runs(lambda invocation: (rerun, "", 0))
    with pytest.raises(ProverToolError, match="workflow run"):
        verify_attestation(_request(tmp_path))


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps([{"attestation": {}, "verificationResult": {"signature": {}}}]),
        json.dumps([
            {
                "attestation": {},
                "verificationResult": {
                    "statement": {
                        "predicate": {
                            "runDetails": {
                                "metadata": {
                                    "invocationId": (
                                        "https://github.com/leynos/rust-prover-tools"
                                        "/actions/runs/12345"
                                    )
                                }
                            }
                        }
                    }
                },
            }
        ]),
    ],
)
def test_a_verified_record_must_name_the_manifest_run(
    tmp_path: Path, cmd_mox: CmdMox, payload: str
) -> None:
    """A record without the run-and-attempt invocation is not correlated."""
    cmd_mox.stub("gh").runs(lambda invocation: (payload, "", 0))
    with pytest.raises(ProverToolError, match="workflow run"):
        verify_attestation(_request(tmp_path))


@pytest.mark.parametrize(
    ("stdout", "match"),
    [
        ("", "invalid JSON"),
        ("[]", "no verified attestation"),
        ("{}", "no verified attestation"),
        ("[1]", "unverified record"),
        ('[{"attestation": {}}]', "unverified record"),
        ("not json", "invalid JSON"),
    ],
)
def test_unverified_attestation_outputs_are_refused(
    tmp_path: Path, cmd_mox: CmdMox, stdout: str, match: str
) -> None:
    """Empty, malformed and unverified responses fail closed."""
    cmd_mox.stub("gh").runs(lambda invocation: (stdout, "", 0))
    with pytest.raises(ProverToolError, match=match):
        verify_attestation(_request(tmp_path))


def test_verifier_failure_is_preserved(tmp_path: Path, cmd_mox: CmdMox) -> None:
    """A nonzero verifier status is not masked as success."""
    cmd_mox.stub("gh").runs(lambda invocation: ("", "denied", 2))
    with pytest.raises(CommandFailedError):
        verify_attestation(_request(tmp_path))


def test_pin_bytes_are_the_reviewed_artifact() -> None:
    """The pin renderer remains the byte source the digest covers."""
    assert pin_bytes(make_pin()).startswith(b"schema_version = 1")
