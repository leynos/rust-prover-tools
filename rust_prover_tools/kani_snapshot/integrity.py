"""Authentication of snapshot manifests, payloads and build attestations.

Nothing is extracted or executed until three checks pass: the manifest bytes
match the reviewed pin, every payload matches its manifest record, and a
GitHub build attestation verifies under an explicit policy. Checksums beside an
archive are a convenience, never an authenticator, so they are verified
against the manifest rather than trusted on their own.
"""

from __future__ import annotations

import dataclasses as dc
import json
import stat
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import is_safe_asset_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.manifest import ParsedManifest
    from rust_prover_tools.kani_snapshot.models import (
        AssetRecord,
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "ATTESTATION_PREDICATE",
    "GITHUB_OIDC_ISSUER",
    "SIGNER_WORKFLOW_PATH",
    "AttestationPolicy",
    "attestation_command",
    "policy_for_pin",
    "verify_asset",
    "verify_attestation",
    "verify_checksums",
    "verify_manifest_digest",
)

#: The trusted OIDC issuer for GitHub Actions attestations.
GITHUB_OIDC_ISSUER: typ.Final[str] = "https://token.actions.githubusercontent.com"

#: The SLSA provenance predicate every snapshot attestation must carry.
ATTESTATION_PREDICATE: typ.Final[str] = "https://slsa.dev/provenance/v1"

#: The only workflow permitted to sign a snapshot.
SIGNER_WORKFLOW_PATH: typ.Final[str] = ".github/workflows/kani-monthly.yml"

#: The only source ref a snapshot may be built from.
TRUSTED_SOURCE_REF: typ.Final[str] = "refs/heads/main"

#: A checksum row is exactly `<digest> <name>`.
_CHECKSUM_FIELDS: typ.Final[int] = 2

#: A SHA-256 digest is 64 lowercase hexadecimal characters.
_SHA256_HEX_LENGTH: typ.Final[int] = 64


@dc.dataclass(frozen=True, slots=True)
class AttestationPolicy:
    """Explicit identity policy applied when verifying an attestation.

    Attributes
    ----------
    repository : str
        Repository the attestation must be linked with.
    signer_workflow : str
        `owner/repo/path` of the signing workflow.
    source_digest : str
        Builder commit enforced as the source digest. Required: an empty value
        would silently weaken the provenance binding.
    signer_digest : str
        Builder commit enforced as the signer workflow digest. Required for the
        same reason.
    certificate_issuer : str
        OIDC issuer enforced on the signing certificate.
    source_ref : str
        Source ref enforced on the provenance statement.
    predicate_type : str
        Required provenance predicate type.
    trust_root : Path | None
        Optional separately provisioned `trusted_root.jsonl`.
    """

    repository: str
    signer_workflow: str
    source_digest: str
    signer_digest: str
    certificate_issuer: str = GITHUB_OIDC_ISSUER
    source_ref: str = TRUSTED_SOURCE_REF
    predicate_type: str = ATTESTATION_PREDICATE
    trust_root: Path | None = None


def policy_for_pin(pin: SnapshotPin) -> AttestationPolicy:
    """Return the fixed attestation policy a pin requires.

    Parameters
    ----------
    pin : SnapshotPin
        Validated pin.

    Returns
    -------
    AttestationPolicy
        Policy binding the canonical repository, workflow, ref and builder SHA.
    """
    return AttestationPolicy(
        repository=pin.repository,
        signer_workflow=f"{pin.repository}/{SIGNER_WORKFLOW_PATH}",
        source_digest=pin.builder_commit,
        signer_digest=pin.builder_commit,
    )


def verify_manifest_digest(parsed: ParsedManifest, pin: SnapshotPin) -> None:
    """Require exact manifest bytes to match the reviewed digest.

    Parameters
    ----------
    parsed : ParsedManifest
        Manifest with its exact-byte digest.
    pin : SnapshotPin
        Reviewed pin naming the approved digest.

    Raises
    ------
    ProverToolError
        Raised when the digest differs.
    """
    if parsed.sha256 != pin.manifest_sha256:
        msg = (
            "snapshot manifest digest does not match the reviewed pin: "
            f"expected {pin.manifest_sha256}, got {parsed.sha256}"
        )
        raise ProverToolError(msg)


def verify_asset(
    path: Path,
    asset: AssetRecord,
    *,
    checkpoint: cabc.Callable[[], None] | None = None,
) -> None:
    """Require a payload to match its recorded size and digest.

    Parameters
    ----------
    path : Path
        Downloaded or local payload.
    asset : AssetRecord
        Manifest record naming the expected bytes.
    checkpoint : collections.abc.Callable[[], None] | None
        Called while the digest is computed, so an installation deadline can
        interrupt a large payload mid-hash.

    Raises
    ------
    ProverToolError
        Raised when the payload is missing, not a regular file, has the wrong
        size, or has a different digest.
    """
    try:
        status = path.stat()
    except OSError as exc:
        msg = f"failed to inspect snapshot asset '{asset.name}': {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot asset '{asset.name}' is not a regular file"
        raise ProverToolError(msg)
    if status.st_size != asset.size:
        msg = (
            f"snapshot asset '{asset.name}' has {status.st_size} bytes, "
            f"expected {asset.size}"
        )
        raise ProverToolError(msg)
    actual = sha256_file(path, checkpoint=checkpoint)
    if actual != asset.sha256:
        msg = (
            f"snapshot asset '{asset.name}' digest does not match the manifest: "
            f"expected {asset.sha256}, got {actual}"
        )
        raise ProverToolError(msg)


def verify_checksums(
    checksums_path: Path,
    manifest: SnapshotManifest,
    manifest_sha256: str,
) -> None:
    """Verify a `SHA256SUMS` file against the manifest and its digest.

    Parameters
    ----------
    checksums_path : Path
        Checksum file to read.
    manifest : SnapshotManifest
        Manifest whose payload digests the file must agree with.
    manifest_sha256 : str
        Digest of the manifest, which must also be listed.

    Raises
    ------
    ProverToolError
        Raised for unreadable, malformed, duplicate or disagreeing rows, or a
        missing manifest or payload row.
    """
    try:
        text = checksums_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"failed to read snapshot checksums '{checksums_path}': {exc}"
        raise ProverToolError(msg) from exc
    expected = {asset.name: asset.sha256 for asset in manifest.assets}
    expected["manifest.json"] = manifest_sha256
    seen: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = _checksum_row(line, checksums_path)
        digest, name = row
        if name in seen:
            msg = f"snapshot checksums list '{name}' more than once"
            raise ProverToolError(msg)
        seen[name] = digest
    for name, digest in expected.items():
        if name not in seen:
            msg = f"snapshot checksums are missing '{name}'"
            raise ProverToolError(msg)
        if seen[name] != digest:
            msg = f"snapshot checksum for '{name}' does not match the manifest"
            raise ProverToolError(msg)


def _checksum_row(line: str, path: Path) -> tuple[str, str]:
    """Parse one checksum row, rejecting malformed or unsafe names."""
    fields = line.split()
    if len(fields) != _CHECKSUM_FIELDS:
        msg = f"snapshot checksum row in '{path}' is not '<digest> <name>'"
        raise ProverToolError(msg)
    digest, name = fields
    if len(digest) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in digest
    ):
        msg = f"snapshot checksum row for '{name}' has an invalid digest"
        raise ProverToolError(msg)
    if not is_safe_asset_name(name):
        msg = f"snapshot checksum row has an unsafe asset name '{name}'"
        raise ProverToolError(msg)
    return digest, name


def attestation_command(
    policy: AttestationPolicy,
    artifact: Path,
    bundle: Path,
) -> tuple[str, ...]:
    """Return the explicit `gh attestation verify` argument vector.

    Parameters
    ----------
    policy : AttestationPolicy
        Identity policy to enforce.
    artifact : Path
        Manifest whose attestation is verified.
    bundle : Path
        Local Sigstore bundle.
    timeout_seconds : float | None
        Budget for the verifier process. `None` leaves it unbounded; a bounded
        verifier is terminated with its process group on expiry.

    Returns
    -------
    tuple[str, ...]
        Argument vector with every policy flag explicit.
    """
    argv = [
        "gh",
        "attestation",
        "verify",
        str(artifact),
        "--bundle",
        str(bundle),
        "--repo",
        policy.repository,
        "--signer-workflow",
        policy.signer_workflow,
        "--cert-oidc-issuer",
        policy.certificate_issuer,
        "--source-ref",
        policy.source_ref,
        "--source-digest",
        policy.source_digest,
        "--signer-digest",
        policy.signer_digest,
        "--predicate-type",
        policy.predicate_type,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    if policy.trust_root is not None:
        argv.extend(("--custom-trusted-root", str(policy.trust_root)))
    return tuple(argv)


def verify_attestation(
    policy: AttestationPolicy,
    artifact: Path,
    bundle: Path,
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, object], ...]:
    """Verify a build attestation and return the verified results.

    Parameters
    ----------
    policy : AttestationPolicy
        Identity policy to enforce.
    artifact : Path
        Manifest whose attestation is verified.
    bundle : Path
        Local Sigstore bundle.
    timeout_seconds : float | None
        Budget for the verifier process. `None` leaves it unbounded; a bounded
        verifier is terminated with its process group on expiry.

    Returns
    -------
    tuple[dict[str, object], ...]
        Non-empty verified result records.

    Raises
    ------
    ProverToolError
        Raised when the verifier fails, prints invalid JSON, or returns no
        verified result.
    """
    result = run_checked(
        CommandSpec(
            attestation_command(policy, artifact, bundle),
            timeout_seconds=timeout_seconds,
        )
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = "gh attestation verify returned invalid JSON"
        raise ProverToolError(msg) from exc
    if not isinstance(payload, list) or not payload:
        msg = "gh attestation verify returned no verified attestation"
        raise ProverToolError(msg)
    records: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict) or "verificationResult" not in item:
            msg = "gh attestation verify returned an unverified record"
            raise ProverToolError(msg)
        records.append(typ.cast("dict[str, object]", item))
    return tuple(records)
