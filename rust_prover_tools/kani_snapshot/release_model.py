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
from rust_prover_tools.kani_snapshot.release_markers import (
    parse_input_marker,
    parse_run_marker,
    release_body,
)
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
    "VerifiedAssets",
    "parse_input_marker",
    "parse_run_marker",
    "release_body",
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
    DISCARD_PLACEHOLDER : str
        Delete an asset record that holds no uploaded bytes, so the next pass
        can upload the real file under that name.
    HASH_ASSETS : str
        Download and hash only the named assets, to learn whether a draft
        that needs an edit may be edited at all. Unlike `VERIFY_ASSETS` this
        makes no provenance claim.
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
    DISCARD_PLACEHOLDER = "discard-placeholder"
    HASH_ASSETS = "hash-assets"
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
    input_fingerprint : str
        Digest of the declared build inputs. Equal fingerprints establish
        equal declared inputs, not equal output bytes: a new run produces a
        new manifest even when its inputs are unchanged. The value is what
        decides whether an existing publication may answer with a verified
        no-op.
    run_id : int
        Trusted workflow run that produced the artefacts.
    run_attempt : int
        Attempt of that run.
    """

    tag: str
    builder_commit: str
    assets: tuple[ReleaseAsset, ...]
    manifest_sha256: str
    input_fingerprint: str
    run_id: int
    run_attempt: int

    def __post_init__(self) -> None:
        """Reject an expected identity that cannot be published safely."""
        if TAG_PATTERN.fullmatch(self.tag) is None:
            msg = "expected release tag must match the immutable snapshot grammar"
            raise ProverToolError(msg)
        require_truthy_text(self.builder_commit, "expected builder commit")
        require_sha256(self.manifest_sha256, "expected manifest digest")
        require_sha256(self.input_fingerprint, "expected input fingerprint")
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
    asset_id : int | None
        Server identifier of the record, used to delete a placeholder left by
        an interrupted upload. `None` when the record was not read from the
        API, as with a synthesised observation in a test.
    """

    name: str
    size: int
    sha256: str | None = None
    asset_id: int | None = None

    @property
    def is_placeholder(self) -> bool:
        """Whether the record holds no uploaded bytes.

        A failed upload can leave the asset record behind with nothing in it.
        The record is still listed, but it contains neither bytes nor a digest,
        so it is a name reservation rather than an asset; only a real upload can
        satisfy the expected identity.
        """
        return self.size == 0 and self.sha256 is None


@dc.dataclass(frozen=True, slots=True)
class VerifiedAssets:
    """The outcome of downloading and authenticating a release's served bytes.

    A state query reports what the API says a release holds. This record is the
    separate answer to "what does it actually serve", produced by the explicit
    verification command so that a download never hides inside a query and a
    caller can see whether any download happened at all.

    Attributes
    ----------
    digests : tuple[tuple[str, str], ...]
        `(name, sha256)` for every downloaded asset, computed from the bytes.
    provenance_verified : bool
        Whether the downloaded evidence authenticated its own manifest: the
        checksums describe it and its payloads, the attestation names its
        digest under the consumer's policy, and its identities reproduce the
        build-input fingerprint the release body records.
    reason : str | None
        Why authentication failed. It is required exactly when the evidence did
        not authenticate, so a refusal can always name its cause.
    """

    digests: tuple[tuple[str, str], ...]
    provenance_verified: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        """Reject a verification result that cannot explain itself."""
        if self.provenance_verified and self.reason is not None:
            msg = "a verified release cannot report a verification failure"
            raise ProverToolError(msg)
        if not self.provenance_verified and self.reason is None:
            msg = "an unverified release must report why its evidence failed"
            raise ProverToolError(msg)

    def digest_map(self) -> dict[str, str]:
        """Return the downloaded digests indexed by asset name."""
        return dict(self.digests)


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
        Commit the tag resolves to. The adapter resolves the tag ref rather
        than trusting the release metadata, which GitHub ignores when the tag
        already exists; for a release whose tag is not created yet (a fresh
        draft), it is the recorded target the tag will be created at.
    assets : tuple[ObservedAsset, ...]
        Assets currently attached to the release.
    release_id : int | None
        API identifier needed to publish or inspect the release.
    body : str | None
        Release body, which records the creating run and the build inputs.
    provenance_verified : bool
        Whether a published release authenticated its own publication: its
        manifest, checksums and payload digests were re-verified from the
        downloaded bytes, its attestation names that manifest, and its
        recorded identities reproduce the build-input fingerprint in the
        release body. The GitHub adapter also verifies the attestation
        signature through the consumer's `gh attestation verify` policy —
        a trusted root, the pinned signer workflow, the manifest's builder
        commit and the recorded run and attempt — and only then sets this
        flag. Publication requires it for a draft as well as a published
        release: refusing an unauthenticated bundle while the release is
        still a draft keeps the failure repairable, where a published release
        can no longer be changed. It is `False` until a verification command
        has been run for this observation.
    provenance_reason : str | None
        Why the served evidence failed to authenticate, when it did. A
        decision can then refuse a release for the actual cause rather than
        reporting only that verification failed.
    """

    exists: bool
    is_draft: bool
    tag: str | None = None
    target_commitish: str | None = None
    assets: tuple[ObservedAsset, ...] = ()
    release_id: int | None = None
    body: str | None = None
    provenance_verified: bool = False
    provenance_reason: str | None = None

    def __post_init__(self) -> None:
        """Reject an impossible observation shape."""
        if not self.exists and any((self.is_draft, bool(self.assets))):
            msg = "an absent release cannot be a draft or carry assets"
            raise ProverToolError(msg)
        if not self.exists and self.provenance_verified:
            msg = "an absent release cannot verify a publication"
            raise ProverToolError(msg)
        if self.provenance_verified and self.provenance_reason is not None:
            msg = "a verified release cannot report a verification failure"
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
    discards : tuple[ObservedAsset, ...]
        Placeholder records a discard action must delete.
    verifications : tuple[str, ...]
        Asset names a verification action must download and hash.
    """

    action: ReleaseAction
    reason: str
    uploads: tuple[ReleaseAsset, ...] = ()
    discards: tuple[ObservedAsset, ...] = ()
    verifications: tuple[str, ...] = ()
