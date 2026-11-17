"""Typed release state and evidence for immutable snapshot publication.

These records describe what a publication attempt expects and what the release
API currently reports; `release_policy` is the only thing that interprets them.
Keeping the shapes separate from the decisions makes the state machine's inputs
and outputs explicit: a caller cannot hand `decide` an unvalidated dictionary,
and cannot mistake an observation for evidence.
"""

from __future__ import annotations

import dataclasses as dc
import enum

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.tags import TAG_PATTERN
from rust_prover_tools.kani_snapshot.validation import (
    require_non_negative_int,
    require_positive_int,
    require_sha256,
    require_truthy_text,
)

__all__ = (
    "ExpectedRelease",
    "ObservedAsset",
    "ObservedRelease",
    "ReleaseAction",
    "ReleaseAsset",
    "ReleaseDecision",
    "ReleaseTransientError",
    "ValidationBinding",
)


class ReleaseTransientError(ProverToolError):
    """A release API failure that a fresh observation may resolve.

    A publication call that times out may still have succeeded, so the caller
    must read state before deciding what happened. Only transport-shaped
    failures use this class; a conflict or a policy refusal is permanent.
    """


class ReleaseAction(enum.Enum):
    """One step the publication state machine may take.

    Attributes
    ----------
    CREATE_DRAFT : str
        Create the draft release at the recorded builder commit.
    UPLOAD_ASSETS : str
        Upload the named missing assets to an existing draft.
    VERIFY_ASSETS : str
        Download and hash assets whose bytes have not been observed yet.
    PUBLISH : str
        Make a complete, verified draft public.
    NOOP : str
        The published release already matches the expected identity.
    CONFLICT : str
        The observed state cannot be reconciled without a new identity.
    """

    CREATE_DRAFT = "create-draft"
    UPLOAD_ASSETS = "upload-assets"
    VERIFY_ASSETS = "verify-assets"
    PUBLISH = "publish"
    NOOP = "noop"
    CONFLICT = "conflict"


@dc.dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """One expected release asset and its exact byte identity.

    Attributes
    ----------
    name : str
        Release asset basename.
    size : int
        Exact size in bytes.
    sha256 : str
        Lowercase SHA-256 of the exact bytes.
    """

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        """Reject a malformed asset identity before it can be compared."""
        require_truthy_text(self.name, "release asset name")
        require_non_negative_int(self.size, "release asset size")
        require_sha256(self.sha256, "release asset digest")


@dc.dataclass(frozen=True, slots=True)
class ExpectedRelease:
    """The immutable release a validated publication attempt expects.

    Attributes
    ----------
    tag : str
        Immutable snapshot tag.
    builder_commit : str
        Full builder commit the draft tag must name.
    assets : tuple[ReleaseAsset, ...]
        Complete expected asset set, in manifest order.
    manifest_sha256 : str
        Digest of the exact manifest bytes the validation receipt names.
    run_id : int
        Trusted workflow run that produced the artefacts.
    run_attempt : int
        Attempt of that run.
    """

    tag: str
    builder_commit: str
    assets: tuple[ReleaseAsset, ...]
    manifest_sha256: str
    run_id: int
    run_attempt: int

    def __post_init__(self) -> None:
        """Reject an expected identity that cannot be published safely."""
        if TAG_PATTERN.fullmatch(self.tag) is None:
            msg = "expected release tag must match the immutable snapshot grammar"
            raise ProverToolError(msg)
        require_truthy_text(self.builder_commit, "expected builder commit")
        require_sha256(self.manifest_sha256, "expected manifest digest")
        require_positive_int(self.run_id, "expected run id")
        require_positive_int(self.run_attempt, "expected run attempt")
        names = [asset.name for asset in self.assets]
        if not names:
            msg = "expected release must name at least one asset"
            raise ProverToolError(msg)
        if len(set(names)) != len(names):
            msg = "expected release repeats an asset name"
            raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class ObservedAsset:
    """One asset the release API currently reports.

    Attributes
    ----------
    name : str
        Asset basename.
    size : int
        Reported size in bytes.
    sha256 : str | None
        Digest computed by re-downloading the bytes, or `None` when the bytes
        have not been observed yet.
    """

    name: str
    size: int
    sha256: str | None = None


@dc.dataclass(frozen=True, slots=True)
class ObservedRelease:
    """The release state read from the API at one moment.

    Attributes
    ----------
    exists : bool
        Whether a release with the expected tag exists.
    is_draft : bool
        Whether an existing release is still a draft.
    tag : str | None
        Tag the release reports.
    target_commitish : str | None
        Commit the release tag targets.
    assets : tuple[ObservedAsset, ...]
        Assets currently attached to the release.
    release_id : int | None
        API identifier needed to publish or inspect the release.
    """

    exists: bool
    is_draft: bool
    tag: str | None = None
    target_commitish: str | None = None
    assets: tuple[ObservedAsset, ...] = ()
    release_id: int | None = None

    def __post_init__(self) -> None:
        """Reject an impossible observation shape."""
        if not self.exists and any((self.is_draft, bool(self.assets))):
            msg = "an absent release cannot be a draft or carry assets"
            raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class ValidationBinding:
    """Evidence that clean validation passed for this exact release.

    Attributes
    ----------
    run_id : int
        Run that produced the validation receipt.
    run_attempt : int
        Attempt of that run.
    manifest_sha256 : str
        Manifest digest the receipt is bound to.
    assets : tuple[str, ...]
        Asset names the receipt recorded.
    verification_passed : bool
        Whether the clean validation evidence is a success.
    """

    run_id: int
    run_attempt: int
    manifest_sha256: str
    assets: tuple[str, ...]
    verification_passed: bool


@dc.dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """The next action and the reason it was chosen.

    Attributes
    ----------
    action : ReleaseAction
        One step of the state machine.
    reason : str
        Human-readable justification, stable enough to assert in tests.
    uploads : tuple[ReleaseAsset, ...]
        Assets an upload action must send.
    verifications : tuple[str, ...]
        Asset names a verification action must download and hash.
    """

    action: ReleaseAction
    reason: str
    uploads: tuple[ReleaseAsset, ...] = ()
    verifications: tuple[str, ...] = ()
