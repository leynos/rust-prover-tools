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
import logging
import typing as typ
import urllib.parse

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.workflow_run import (
    WorkflowRun,
    require_workflow_run,
)
from rust_prover_tools.observability import operation_span

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotPin, WorkflowIdentity

__all__ = (
    "ATTESTATION_PREDICATE",
    "GITHUB_OIDC_ISSUER",
    "SIGNER_WORKFLOW_PATH",
    "AttestationPolicy",
    "AttestationRequest",
    "WorkflowRun",
    "attestation_command",
    "policy_for_pin",
    "require_workflow_run",
    "run_from_invocation",
    "verify_attestation",
)


_LOGGER = logging.getLogger(__name__)

#: The trusted OIDC issuer for GitHub Actions attestations.
GITHUB_OIDC_ISSUER: typ.Final[str] = "https://token.actions.githubusercontent.com"

#: The SLSA provenance predicate every snapshot attestation must carry.
ATTESTATION_PREDICATE: typ.Final[str] = "https://slsa.dev/provenance/v1"

#: The only workflow permitted to sign a snapshot.
SIGNER_WORKFLOW_PATH: typ.Final[str] = ".github/workflows/kani-monthly.yml"

#: The only source ref a snapshot may be built from.
TRUSTED_SOURCE_REF: typ.Final[str] = "refs/heads/main"


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
    # The verifier is an external process, so its boundary is timed separately
    # from the correlation work that follows it.
    with operation_span(_LOGGER, "kani.snapshot.attestation"):
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
    runs = tuple(
        run
        for record in records
        if (run := _record_run(record, request.policy.repository)) is not None
    )
    require_workflow_run(runs, request.workflow)
    return tuple(records)


def _record_run(
    record: dict[str, object],
    repository: str,
) -> WorkflowRun | None:
    """Return the run one verified record names, or `None`."""
    invocation = _verified_invocation(record)
    if invocation is None:
        return None
    return run_from_invocation(invocation, repository)


def _run_attempt_segments(
    invocation: str,
    repository: str,
) -> tuple[str, str] | None:
    """Return the run and attempt segments of a canonical invocation URL.

    Parameters
    ----------
    invocation : str
        `invocationId` value from a verified provenance statement.
    repository : str
        `owner/repo` the invocation must belong to.

    Returns
    -------
    tuple[str, str] | None
        The run and attempt path segments, or `None` when the value is not
        this repository's canonical run-and-attempt URL.
    """
    parsed = urllib.parse.urlsplit(invocation)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    if parsed.query or parsed.fragment:
        return None
    parts = parsed.path.strip("/").split("/")
    prefix = [*repository.split("/"), "actions", "runs"]
    if parts[: len(prefix)] != prefix or len(parts) != len(prefix) + 3:
        return None
    if parts[len(prefix) + 1] != "attempts":
        return None
    return parts[len(prefix)], parts[len(prefix) + 2]


def run_from_invocation(
    invocation: str,
    repository: str,
) -> WorkflowRun | None:
    """Parse one provenance invocation URL into the run it names.

    The verifier's output is external data, so a value that is not this
    repository's canonical run-and-attempt URL names no run rather than
    raising. Parsing lives in this adapter so the domain model compares runs
    without knowing the verifier's URL syntax.

    Parameters
    ----------
    invocation : str
        `invocationId` value from a verified provenance statement.
    repository : str
        `owner/repo` the invocation must belong to.

    Returns
    -------
    WorkflowRun | None
        The named run, or `None` when the value is not this repository's
        canonical run-and-attempt URL.
    """
    segments = _run_attempt_segments(invocation, repository)
    if segments is None:
        return None
    try:
        run_id, attempt = (int(segment) for segment in segments)
    except ValueError:
        return None
    if run_id <= 0 or attempt <= 0:
        return None
    return WorkflowRun(run_id=run_id, run_attempt=attempt)
