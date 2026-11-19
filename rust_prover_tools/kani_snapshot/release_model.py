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
import re
import typing as typ

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
    "parse_input_marker",
    "parse_run_marker",
    "release_body",
)

#: Machine-readable record of the run that created a draft.
_RUN_MARKER: typ.Final[re.Pattern[str]] = re.compile(
    r"<!-- kani-snapshot-run: (?P<run_id>[1-9]\d*)/(?P<run_attempt>[1-9]\d*) -->"
)

#: Machine-readable record of the build inputs a release was created from.
_INPUT_MARKER: typ.Final[re.Pattern[str]] = re.compile(
    r"<!-- kani-snapshot-input: (?P<fingerprint>[0-9a-f]{64}) -->"
)


def release_body(
    tag: str,
    fingerprint: str,
    *,
    run_id: int,
    run_attempt: int,
) -> str:
    """Return the release body that records a release's origin.

    A draft may be resumed only by the run and attempt that created it, and may
    be published as a no-op only by a run with the same build inputs. Both
    facts are written into the body so an observer can read them back without
    downloading assets, and so they survive publication unchanged.
    """
    return (
        f"Immutable Kani snapshot {tag}\n\n"
        f"<!-- kani-snapshot-input: {fingerprint} -->\n"
        f"<!-- kani-snapshot-run: {run_id}/{run_attempt} -->\n"
    )


def parse_run_marker(body: str | None) -> tuple[int, int] | None:
    """Return the run and attempt a release body records, when it does."""
    if body is None:
        return None
    match = _RUN_MARKER.search(body)
    if match is None:
        return None
    return int(match.group("run_id")), int(match.group("run_attempt"))


def parse_input_marker(body: str | None) -> str | None:
    """Return the build-input fingerprint a release body records, when it does."""
    if body is None:
        return None
    match = _INPUT_MARKER.search(body)
    return None if match is None else match.group("fingerprint")


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
    input_fingerprint : str
        Digest of the declared build inputs. Two runs with the same value
        would rebuild the same bytes, so an existing publication may answer
        with a verified no-op.
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
    body : str | None
        Release body, which records the creating run and the build inputs.
    provenance_verified : bool
        Whether a published release's own manifest, checksums and payload
        digests were re-verified from the downloaded bytes. `False` for a
        draft, which has no complete set to verify yet.
    """

    exists: bool
    is_draft: bool
    tag: str | None = None
    target_commitish: str | None = None
    assets: tuple[ObservedAsset, ...] = ()
    release_id: int | None = None
    body: str | None = None
    provenance_verified: bool = False

    def __post_init__(self) -> None:
        """Reject an impossible observation shape."""
        if not self.exists and any((self.is_draft, bool(self.assets))):
            msg = "an absent release cannot be a draft or carry assets"
            raise ProverToolError(msg)
        if self.is_draft and self.provenance_verified:
            msg = "a draft has no complete publication to verify"
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
