"""Pure release-state policy for immutable snapshot publication.

Publication is a state machine over one immutable tag, never a sequence of
unchecked writes. The states that matter are absent, a draft release, a draft
whose assets are incomplete or unverified, a published release that matches the
expected identity, and a conflict that must be diagnosed rather than repaired
in place. The state shapes live in `release_model`; this module is the only
place that interprets them.

The lemma this module exists for is narrow and testable: `decide` never returns
`PUBLISH` while an expected asset is absent, has the wrong size, has a digest
that has not been observed, or while the draft's own served evidence has not
authenticated its manifest; it never returns `NOOP` for a published
release whose recorded inputs differ or whose own publication did not verify;
and it never returns an action that would delete, replace or move anything
once a release is published. Recovery from an interrupted attempt is expressed
by re-observing state, not by repeating a write, so a publication that timed
out after succeeding is recognised as a no-op on the next observation.

A published release is compared by its recorded build-input fingerprint, not
by the new run's asset bytes. A rebuild of unchanged inputs still produces a
new manifest — it records a new run, attempt, cache outcome and timings — so
byte equality could never hold. The published release's own bytes are instead
verified against its own manifest, which the adapter reports as
`ObservedRelease.provenance_verified`.
"""

from __future__ import annotations

from rust_prover_tools.kani_snapshot.release_assets_policy import (
    classify_assets,
    first_reason,
    unverified_message,
)
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAction,
    ReleaseAsset,
    ReleaseDecision,
    ReleaseTransientError,
    ValidationBinding,
    VerifiedAssets,
    parse_input_marker,
    parse_run_marker,
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
    "decide",
)


def _published_conflict(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> str | None:
    """Return why a published release differs from the expected identity."""
    return first_reason((
        (
            observed.tag != expected.tag,
            "published release reports another tag",
        ),
        (
            observed.target_commitish not in {None, expected.builder_commit},
            "published release names another builder commit",
        ),
    ))


def _binding_conflict(
    expected: ExpectedRelease,
    validation: ValidationBinding,
) -> str | None:
    """Return why validation evidence does not bind, or `None` when it does."""
    checks = (
        (
            not validation.verification_passed,
            "clean validation did not pass",
        ),
        (
            (validation.run_id, validation.run_attempt)
            != (expected.run_id, expected.run_attempt),
            "validation receipt belongs to another workflow run or attempt",
        ),
        (
            validation.manifest_sha256 != expected.manifest_sha256,
            "validation receipt names another manifest digest",
        ),
        (
            set(validation.assets) != {asset.name for asset in expected.assets},
            "validation receipt records an incomplete asset set",
        ),
    )
    return first_reason(checks)


def _draft_binding_conflict(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> str | None:
    """Return why a draft does not belong to this run, or `None` when it does."""
    run = parse_run_marker(observed.body)
    fingerprint = parse_input_marker(observed.body)
    return first_reason((
        (
            run is None,
            "draft does not record the run and attempt that created it",
        ),
        (
            run != (expected.run_id, expected.run_attempt),
            "draft was created by another workflow run or attempt",
        ),
        (
            fingerprint is None,
            "draft does not record its build inputs",
        ),
        (
            fingerprint != expected.input_fingerprint,
            "draft was created from different build inputs",
        ),
    ))


def _classify_draft(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Refuse a draft whose origin differs, else classify its assets."""
    conflict = _draft_binding_conflict(expected, observed)
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
    if observed.tag != expected.tag:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            "draft reports another tag",
        )
    if observed.target_commitish not in {None, expected.builder_commit}:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            "draft targets another builder commit",
        )
    return classify_assets(expected, observed)


def _classify_published(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Treat a published release as a no-op only when it verifies.

    The expected asset names are compared because they are derived from the
    same inputs, but their bytes are not: this run assembled a new manifest,
    so only the published release's own evidence can authenticate its bytes.

    The recorded inputs and the asset names are decided from the observation
    itself, so they are checked before any download is requested. A release
    that records other inputs is already refused; asking first for a download
    whose transfer could fail would report a transport error in place of the
    conflict the release notes already establish. Only a release whose
    metadata still matches is asked for the bytes that authenticate it. An
    observation that has not yet been verified asks for verification rather
    than reporting a conflict; only a completed verification that failed is a
    conflict.
    """
    fingerprint = parse_input_marker(observed.body)
    conflict = first_reason((
        (
            fingerprint is None,
            "published release does not record its build inputs",
        ),
        (
            fingerprint != expected.input_fingerprint,
            "published release records different build inputs",
        ),
        (
            {asset.name for asset in observed.assets}
            != {asset.name for asset in expected.assets},
            "published release has a different asset set",
        ),
    ))
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
    unverified = tuple(
        sorted(asset.name for asset in observed.assets if asset.sha256 is None)
    )
    if unverified:
        return ReleaseDecision(
            ReleaseAction.VERIFY_ASSETS,
            "published assets have not been re-downloaded and hashed",
            verifications=unverified,
        )
    if observed.provenance_verified:
        return ReleaseDecision(
            ReleaseAction.NOOP,
            "published release records the same inputs and verifies "
            "against its manifest",
        )
    if observed.provenance_reason is None:
        # The identity and asset set agree, but no command has downloaded the
        # release's own bytes yet. Requiring verification before a no-op keeps
        # "already published" an evidence-backed claim rather than a reading of
        # the API's metadata.
        return ReleaseDecision(
            ReleaseAction.VERIFY_ASSETS,
            "published release's evidence has not been re-verified",
            verifications=tuple(sorted(asset.name for asset in observed.assets)),
        )
    return ReleaseDecision(
        ReleaseAction.CONFLICT,
        unverified_message(
            "published release did not authenticate its own publication",
            observed.provenance_reason,
        ),
    )


def decide(
    expected: ExpectedRelease,
    observed: ObservedRelease,
    validation: ValidationBinding,
) -> ReleaseDecision:
    """Return the next publication action for one observed release state.

    Parameters
    ----------
    expected : ExpectedRelease
        Immutable identity the attempt must publish.
    observed : ObservedRelease
        Current API state, including digests of any assets that exist.
    validation : ValidationBinding
        Evidence from the same run and attempt that clean validation passed.

    Returns
    -------
    ReleaseDecision
        The next action and its reason.
    """
    conflict = _binding_conflict(expected, validation)
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
    if not observed.exists:
        return ReleaseDecision(
            ReleaseAction.CREATE_DRAFT,
            "no release exists for the immutable tag",
        )
    if observed.is_draft:
        return _classify_draft(expected, observed)
    published_conflict = _published_conflict(expected, observed)
    if published_conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, published_conflict)
    return _classify_published(expected, observed)
