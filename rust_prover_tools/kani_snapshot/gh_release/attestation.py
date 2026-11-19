"""Attestation checks for a published release's own evidence.

A published release authenticates itself with an in-toto statement about its
manifest. This module owns both halves of that check: the cheap structural
refusal that rejects an empty bundle, an unrelated statement or a statement
about some other digest before a verifier process starts, and the signature
verification under the consumer's own `gh attestation verify` policy.

Only the second half can establish authenticity; the structural checks run
first as a bounded refusal and never decide `provenance_verified` on their own.
"""

from __future__ import annotations

import base64
import typing as typ
from pathlib import PurePosixPath

from rust_prover_tools.commands import CommandTimeoutError
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.attestation import (
    ATTESTATION_PREDICATE,
    SIGNER_WORKFLOW_PATH,
    AttestationPolicy,
    AttestationRequest,
    verify_attestation,
)
from rust_prover_tools.kani_snapshot.producer.asset_names import (
    ATTESTATION_NAME,
    MANIFEST_NAME,
)
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_mapping,
    require_sequence,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.manifest import ParsedManifest

__all__ = ("require_attestation", "require_verified_signature")

#: Budget for one `gh attestation verify` run against a published release.
_ATTESTATION_TIMEOUT_SECONDS: typ.Final[float] = 300.0


#: In-toto statement versions GitHub's attestation action emits.
_STATEMENT_TYPES: typ.Final[frozenset[str]] = frozenset({
    "https://in-toto.io/Statement/v1",
    "https://in-toto.io/Statement/v0.1",
})


def _attestation_statement(directory: Path) -> dict[str, object]:
    """Return the in-toto statement a release's attestation bundle carries."""
    bundle = require_mapping(
        strict_json_loads(
            (directory / ATTESTATION_NAME).read_bytes(), "attestation bundle"
        ),
        "attestation bundle",
    )
    envelope = require_mapping(bundle.get("dsseEnvelope"), "attestation envelope")
    payload = envelope.get("payload")
    signatures = envelope.get("signatures")
    if not isinstance(payload, str) or not payload:
        msg = "attestation bundle carries no DSSE payload"
        raise ProverToolError(msg)
    if not isinstance(signatures, list) or not signatures:
        msg = "attestation bundle carries no signature"
        raise ProverToolError(msg)
    try:
        statement = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        msg = f"attestation payload is not base64: {exc}"
        raise ProverToolError(msg) from exc
    return require_mapping(
        strict_json_loads(statement, "attestation statement"),
        "attestation statement",
    )


def _subject_names_manifest(subject: dict[str, object], manifest_sha256: str) -> bool:
    """Return whether one subject record names the manifest's exact bytes."""
    digest = subject.get("digest")
    if not isinstance(digest, dict):
        return False
    if typ.cast("dict[str, object]", digest).get("sha256") != manifest_sha256:
        return False
    name = subject.get("name")
    if not isinstance(name, str):
        return False
    return PurePosixPath(name).name == MANIFEST_NAME


def require_attestation(directory: Path, manifest_sha256: str) -> None:
    """Require the attestation to be a well-formed statement about the manifest.

    This is the cheap half of the check: it rejects an empty bundle, an
    unrelated statement and a statement about some other digest before a
    verifier process is started. It never establishes authenticity on its own;
    `_require_verified_signature` does that, and it alone decides
    `provenance_verified`.
    """
    statement = _attestation_statement(directory)
    statement_type = statement.get("_type")
    # A non-string `_type` is as invalid as an unknown one; membership alone
    # would raise an unhashable-type error for an array instead of refusing it.
    if not isinstance(statement_type, str) or statement_type not in _STATEMENT_TYPES:
        msg = "attestation statement is not an in-toto statement"
        raise ProverToolError(msg)
    if statement.get("predicateType") != ATTESTATION_PREDICATE:
        msg = "attestation statement is not build provenance"
        raise ProverToolError(msg)
    subjects = [
        require_mapping(subject, "attestation subject")
        for subject in require_sequence(statement.get("subject"), "attestation subject")
    ]
    if not any(
        _subject_names_manifest(subject, manifest_sha256) for subject in subjects
    ):
        msg = "attestation does not cover the published manifest"
        raise ProverToolError(msg)


def require_verified_signature(directory: Path, parsed: ParsedManifest) -> None:
    """Require the bundle to authenticate under the consumer's own policy.

    The policy is the one a consumer installs with: this repository's signing
    workflow, the trusted source ref, and the manifest's builder commit as both
    source and signer digest. The verifier also has to name this manifest's run
    and attempt, so an attestation for another run cannot authenticate these
    bytes. Anything short of that raises, and the caller reports the release as
    unverified rather than trusting the structural checks alone.
    """
    # The verifier raises unless it returns at least one authenticated
    # statement for this run, so its return value carries no further decision.
    try:
        verify_attestation(
            AttestationRequest(
                policy=AttestationPolicy(
                    repository=parsed.manifest.repository,
                    signer_workflow=(
                        f"{parsed.manifest.repository}/{SIGNER_WORKFLOW_PATH}"
                    ),
                    source_digest=parsed.manifest.builder_commit,
                    signer_digest=parsed.manifest.builder_commit,
                ),
                artifact=directory / MANIFEST_NAME,
                bundle=directory / ATTESTATION_NAME,
                workflow=parsed.manifest.workflow,
            ),
            timeout_seconds=_ATTESTATION_TIMEOUT_SECONDS,
        )
    except CommandTimeoutError as exc:
        # A verifier that ran out of time says nothing about the release, so
        # the observation is retryable rather than unverified. A rejected
        # signature stays a `CommandFailedError` and is reported as False.
        msg = "attestation verification timed out; retry the observation"
        raise ReleaseTransientError(msg) from exc
