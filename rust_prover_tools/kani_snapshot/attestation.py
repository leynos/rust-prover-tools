"""Verification of the GitHub build attestation a snapshot must carry.

The verifier's exit status only proves that some attestation satisfied the
policy flags. This module therefore owns both halves of the check: it runs
`gh attestation verify` with every policy decision explicit, and it correlates
the verified statement with the workflow run the manifest records, so an
attestation for another run, or for a rerun of the same run, authenticates
nothing. Certificate identity and source digest are pinned by the policy flags;
the correlation closes the remaining gap between "a trusted builder signed
something" and "that builder signed this run's statement".
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotPin, WorkflowIdentity

__all__ = (
    "ATTESTATION_PREDICATE",
    "GITHUB_OIDC_ISSUER",
    "SIGNER_WORKFLOW_PATH",
    "AttestationPolicy",
    "AttestationRequest",
    "attestation_command",
    "policy_for_pin",
    "verify_attestation",
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


def _expected_invocation(policy: AttestationPolicy, workflow: WorkflowIdentity) -> str:
    """Return the run invocation URL a manifest's own attestation must name."""
    return (
        f"https://github.com/{policy.repository}"
        f"/actions/runs/{workflow.run_id}/attempts/{workflow.run_attempt}"
    )


def _member(value: object, key: str) -> object:
    """Return `key` from a JSON object value, or `None` if it is not one."""
    if not isinstance(value, dict):
        return None
    return typ.cast("dict[str, object]", value).get(key)


def _verified_invocation(record: dict[str, object]) -> str | None:
    """Return the run invocation a verified record names, or `None`.

    The path is the SLSA provenance v1 `runDetails.metadata.invocationId`
    GitHub fills with the run and attempt URL. Every step is type-checked
    because the verifier's output is external data, and a record that does not
    carry the field names no run rather than raising.
    """
    predicate = _member(
        _member(_member(record, "verificationResult"), "statement"), "predicate"
    )
    invocation = _member(
        _member(_member(predicate, "runDetails"), "metadata"), "invocationId"
    )
    return invocation if isinstance(invocation, str) else None


@dc.dataclass(frozen=True, slots=True)
class AttestationRequest:
    """The attestation one installation must verify, and the run it must name.

    Attributes
    ----------
    policy : AttestationPolicy
        Identity policy to enforce.
    artifact : Path
        Manifest whose attestation is verified.
    bundle : Path
        Local Sigstore bundle.
    workflow : WorkflowIdentity
        Workflow run and attempt the manifest records.
    """

    policy: AttestationPolicy
    artifact: Path
    bundle: Path
    workflow: WorkflowIdentity


def verify_attestation(
    request: AttestationRequest,
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, object], ...]:
    """Verify a build attestation for one recorded workflow run.

    The verifier's exit status only proves that some attestation satisfied the
    policy flags; it says nothing about which run the manifest names. The
    verified statement is therefore correlated with the manifest's own workflow
    identity before the result is accepted. Certificate identity and source
    digest are already pinned by the policy flags, so this comparison closes
    the remaining gap: an attestation for a different run, or a rerun of the
    same run, cannot authenticate a manifest that records another attempt.

    Parameters
    ----------
    request : AttestationRequest
        Policy, artefact, bundle and recorded workflow run to verify.
    timeout_seconds : float | None
        Budget for the verifier process. `None` leaves it unbounded; a bounded
        verifier is terminated with its process group on expiry.

    Returns
    -------
    tuple[dict[str, object], ...]
        Non-empty verified result records, at least one naming the request's
        workflow.

    Raises
    ------
    ProverToolError
        Raised when the verifier fails, prints invalid JSON, returns no
        verified result, or verifies no attestation of the recorded run.
    """
    result = run_checked(
        CommandSpec(
            attestation_command(request.policy, request.artifact, request.bundle),
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
    expected = _expected_invocation(request.policy, request.workflow)
    if not any(_verified_invocation(record) == expected for record in records):
        msg = (
            "gh attestation verify did not verify the manifest's workflow run "
            f"{request.workflow.run_id} attempt {request.workflow.run_attempt}"
        )
        raise ProverToolError(msg)
    return tuple(records)
