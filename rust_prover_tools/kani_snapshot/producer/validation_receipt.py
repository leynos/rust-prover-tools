"""Clean-validation evidence for one assembled snapshot.

The receipt is what releases the publish job: it says that the exact manifest
bytes this run assembled were validated, and it names the run and attempt that
performed the validation. Publication refuses a receipt from another run, a
different manifest digest or an incomplete asset set, so a receipt uploaded by
an earlier job cannot authorise a later release.

The receipt is evidence, not a trust anchor. It is written by the same trusted
workflow that assembled the manifest, and its digest binds it to the manifest
bytes; a consumer still verifies the published attestation.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import (
    is_safe_asset_name,
    reject_unknown_keys,
)
from rust_prover_tools.kani_snapshot.release_policy import ValidationBinding
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import TAG_PATTERN
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_positive_int,
    require_sequence,
    require_sha256,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.manifest import ParsedManifest
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        WorkflowIdentity,
    )

__all__ = (
    "RECEIPT_SCHEMA_VERSION",
    "ValidationReceipt",
    "build_receipt",
    "parse_receipt",
    "receipt_to_json",
    "require_same_run",
    "verify_payloads",
    "verify_receipt",
)

#: The only receipt schema version this module understands.
RECEIPT_SCHEMA_VERSION: typ.Final[int] = 1

_RECEIPT_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "run_id",
    "run_attempt",
    "tag",
    "manifest_sha256",
    "assets",
    "verification_passed",
    "checks",
})


@dc.dataclass(frozen=True, slots=True)
class ValidationReceipt:
    """Evidence that clean validation passed for one manifest.

    Attributes
    ----------
    schema_version : int
        Receipt schema version. Only version 1 is accepted.
    run_id : int
        Positive workflow run identifier.
    run_attempt : int
        Positive attempt of that run.
    tag : str
        Immutable snapshot tag the manifest names.
    manifest_sha256 : str
        Digest of the exact validated manifest bytes.
    assets : tuple[str, ...]
        Asset basenames the validation covered.
    verification_passed : bool
        Whether every validation check passed.
    checks : tuple[str, ...]
        Names of the checks that produced the verdict.
    """

    schema_version: int
    run_id: int
    run_attempt: int
    tag: str
    manifest_sha256: str
    assets: tuple[str, ...]
    verification_passed: bool
    checks: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject a receipt that cannot authorise publication."""
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            msg = f"validation receipt schema_version must be {RECEIPT_SCHEMA_VERSION}"
            raise ProverToolError(msg)
        require_positive_int(self.run_id, "validation run_id")
        require_positive_int(self.run_attempt, "validation run_attempt")
        tag = require_truthy_text(self.tag, "validation tag")
        if TAG_PATTERN.fullmatch(tag) is None:
            msg = "validation tag must match the immutable snapshot grammar"
            raise ProverToolError(msg)
        require_sha256(self.manifest_sha256, "validation manifest_sha256")
        if not self.assets:
            msg = "validation receipt must name at least one asset"
            raise ProverToolError(msg)
        if len(set(self.assets)) != len(self.assets):
            msg = "validation receipt repeats an asset name"
            raise ProverToolError(msg)
        if not all(is_safe_asset_name(name) for name in self.assets):
            msg = "validation receipt asset names must be plain basenames"
            raise ProverToolError(msg)
        if not self.checks:
            msg = "validation receipt must record at least one check"
            raise ProverToolError(msg)

    def binding(self) -> ValidationBinding:
        """Return the release-policy binding this receipt justifies."""
        return ValidationBinding(
            run_id=self.run_id,
            run_attempt=self.run_attempt,
            manifest_sha256=self.manifest_sha256,
            assets=self.assets,
            verification_passed=self.verification_passed,
        )


def _names(manifest: SnapshotManifest) -> tuple[str, ...]:
    """Return the manifest's asset names in manifest order."""
    return tuple(asset.name for asset in manifest.assets)


def build_receipt(
    workflow: WorkflowIdentity,
    parsed: ParsedManifest,
    checks: tuple[str, ...],
    *,
    verification_passed: bool,
) -> ValidationReceipt:
    """Build the receipt for one validated manifest.

    Parameters
    ----------
    workflow : WorkflowIdentity
        Trusted run that performed the validation.
    parsed : ParsedManifest
        Parsed manifest whose exact bytes were validated.
    checks : tuple[str, ...]
        Names of the checks that produced the verdict.
    verification_passed : bool
        Whether every check passed.

    Returns
    -------
    ValidationReceipt
        Receipt bound to the manifest bytes and the validating run.
    """
    return ValidationReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id=workflow.run_id,
        run_attempt=workflow.run_attempt,
        tag=parsed.manifest.tag,
        manifest_sha256=parsed.sha256,
        assets=_names(parsed.manifest),
        verification_passed=verification_passed,
        checks=checks,
    )


def require_same_run(receipt: ValidationReceipt, workflow: WorkflowIdentity) -> None:
    """Require a receipt written by the current run and attempt.

    Parameters
    ----------
    receipt : ValidationReceipt
        Receipt read from a previous job.
    workflow : WorkflowIdentity
        Run that is about to use or rewrite the receipt.

    Raises
    ------
    ProverToolError
        Raised when the receipt belongs to another run or attempt. A rerun
        revalidates; it never inherits the previous attempt's verdict.
    """
    if (receipt.run_id, receipt.run_attempt) != (
        workflow.run_id,
        workflow.run_attempt,
    ):
        msg = "validation receipt belongs to another workflow run or attempt"
        raise ProverToolError(msg)


def verify_receipt(receipt: ValidationReceipt, parsed: ParsedManifest) -> None:
    """Require a receipt to describe exactly the parsed manifest.

    Parameters
    ----------
    receipt : ValidationReceipt
        Receipt to check.
    parsed : ParsedManifest
        Manifest bytes read by the publishing job.

    Raises
    ------
    ProverToolError
        Raised when the tag, digest or asset set differs, or when the receipt
        recorded a failed validation.
    """
    if not receipt.verification_passed:
        msg = "validation receipt records a failed validation"
        raise ProverToolError(msg)
    if receipt.tag != parsed.manifest.tag:
        msg = "validation receipt names another tag"
        raise ProverToolError(msg)
    if receipt.manifest_sha256 != parsed.sha256:
        msg = "validation receipt names another manifest digest"
        raise ProverToolError(msg)
    if set(receipt.assets) != set(_names(parsed.manifest)):
        msg = "validation receipt records an incomplete asset set"
        raise ProverToolError(msg)


def verify_payloads(
    parsed: ParsedManifest,
    payloads: Path,
) -> None:
    """Require every payload file to match the manifest's measured bytes.

    Parameters
    ----------
    parsed : ParsedManifest
        Parsed manifest naming the assets.
    payloads : Path
        Directory holding the payload files.

    Raises
    ------
    ProverToolError
        Raised when a payload is missing, is not a regular file, or has a size
        or digest other than the one the manifest records.
    """
    for asset in parsed.manifest.assets:
        path = payloads / asset.name
        if not path.is_file():
            msg = f"validated payload {asset.name!r} is not a regular file"
            raise ProverToolError(msg)
        try:
            size = path.stat().st_size
        except OSError as exc:
            msg = f"failed to stat payload '{path}': {exc}"
            raise ProverToolError(msg) from exc
        if size != asset.size:
            msg = f"payload {asset.name!r} size does not match the manifest"
            raise ProverToolError(msg)
        if sha256_file(path) != asset.sha256:
            msg = f"payload {asset.name!r} digest does not match the manifest"
            raise ProverToolError(msg)


def receipt_to_json(receipt: ValidationReceipt) -> bytes:
    """Render a receipt as deterministic JSON bytes."""
    document = {
        "schema_version": receipt.schema_version,
        "run_id": receipt.run_id,
        "run_attempt": receipt.run_attempt,
        "tag": receipt.tag,
        "manifest_sha256": receipt.manifest_sha256,
        "assets": list(receipt.assets),
        "verification_passed": receipt.verification_passed,
        "checks": list(receipt.checks),
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def _texts(value: object, field: str) -> tuple[str, ...]:
    """Parse a non-empty list of unique non-blank strings."""
    items = tuple(
        require_truthy_text(item, field) for item in require_sequence(value, field)
    )
    if not items:
        msg = f"{field} must not be empty"
        raise ProverToolError(msg)
    if len(set(items)) != len(items):
        msg = f"{field} must not repeat an entry"
        raise ProverToolError(msg)
    return items


def parse_receipt(data: bytes) -> ValidationReceipt:
    """Parse exact receipt bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes as written by this module.

    Returns
    -------
    ValidationReceipt
        Validated receipt.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, wrong types, or a
        receipt that cannot authorise publication.
    """
    document = require_mapping(strict_json_loads(data, "validation receipt"), "receipt")
    reject_unknown_keys(document, _RECEIPT_KEYS, "validation receipt")
    missing = sorted(_RECEIPT_KEYS - set(document))
    if missing:
        msg = f"validation receipt is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    passed = document["verification_passed"]
    if not isinstance(passed, bool):
        msg = "validation receipt verification_passed must be a boolean"
        raise ProverToolError(msg)
    return ValidationReceipt(
        schema_version=require_int(
            document["schema_version"], "validation receipt schema_version"
        ),
        run_id=require_positive_int(document["run_id"], "validation run_id"),
        run_attempt=require_positive_int(
            document["run_attempt"], "validation run_attempt"
        ),
        tag=require_truthy_text(document["tag"], "validation tag"),
        manifest_sha256=require_sha256(
            document["manifest_sha256"], "validation manifest_sha256"
        ),
        assets=_texts(document["assets"], "validation assets"),
        verification_passed=passed,
        checks=_texts(document["checks"], "validation checks"),
    )
