"""The release policy alone decides what a publication state means.

These witnesses hold the lemma the policy exists for: `PUBLISH` is never chosen
while an expected asset is absent, the wrong size, unhashed, or while the
draft's own served evidence does not authenticate its manifest; `NOOP` is never
chosen for a published release whose recorded inputs differ or whose own bytes
were not re-verified; and no decision ever deletes, replaces or moves a
published asset. A published release is compared by the input fingerprint in its
body rather than by this run's asset bytes, because a rebuild of unchanged
inputs still produces a new manifest. Each case names the one property that
makes the state safe to act on, so a regression points at the rule that broke.
"""

from __future__ import annotations

import dataclasses as dc

from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedAsset,
    ObservedRelease,
    ReleaseAction,
    decide,
)
from tests import kani_publish_fixtures as fixtures


def _absent() -> ObservedRelease:
    """Return the absent state."""
    return ObservedRelease(exists=False, is_draft=False)


def test_an_absent_release_creates_a_draft() -> None:
    """No release is the only state that creates one."""
    decision = decide(fixtures.expected(), _absent(), fixtures.validation())
    assert decision.action is ReleaseAction.CREATE_DRAFT


def test_cross_run_validation_cannot_authorise_publication() -> None:
    """A receipt from another attempt is refused before any state is read."""
    decision = decide(
        fixtures.expected(),
        _absent(),
        dc.replace(fixtures.validation(), run_attempt=2),
    )
    assert decision.action is ReleaseAction.CONFLICT
    assert "another workflow run" in decision.reason


def test_failed_validation_cannot_authorise_publication() -> None:
    """A failed clean validation is not evidence, whatever else matches."""
    decision = decide(
        fixtures.expected(),
        _absent(),
        dc.replace(fixtures.validation(), verification_passed=False),
    )
    assert decision.action is ReleaseAction.CONFLICT
    assert "did not pass" in decision.reason


def test_a_receipt_for_another_manifest_is_refused() -> None:
    """The receipt must name the exact manifest bytes being published."""
    decision = decide(
        fixtures.expected(),
        _absent(),
        dc.replace(fixtures.validation(), manifest_sha256="d" * 64),
    )
    assert decision.action is ReleaseAction.CONFLICT
    assert "another manifest digest" in decision.reason


def test_an_incomplete_receipt_is_refused() -> None:
    """A receipt that omits an asset does not cover the release."""
    decision = decide(
        fixtures.expected(),
        _absent(),
        dc.replace(fixtures.validation(), assets=("backend.tar.gz",)),
    )
    assert decision.action is ReleaseAction.CONFLICT
    assert "incomplete asset set" in decision.reason


def test_a_draft_missing_assets_uploads_exactly_those_assets() -> None:
    """A naive publisher could mistake a draft's existence for readiness."""
    name = "backend.tar.gz"
    present = (
        fixtures.observed_asset(name, sha256=fixtures.digest(fixtures.PAYLOADS[name])),
    )
    decision = decide(
        fixtures.expected(), fixtures.draft(present), fixtures.validation()
    )
    assert decision.action is ReleaseAction.UPLOAD_ASSETS
    assert {asset.name for asset in decision.uploads} == set(fixtures.PAYLOADS) - {name}


def test_a_draft_with_unhashed_assets_is_verified_before_publishing() -> None:
    """An asset's presence is a claim until its bytes are re-downloaded."""
    unhashed = tuple(
        fixtures.observed_asset(name, sha256=None) for name in fixtures.PAYLOADS
    )
    decision = decide(
        fixtures.expected(), fixtures.draft(unhashed), fixtures.validation()
    )
    assert decision.action is ReleaseAction.VERIFY_ASSETS
    assert decision.verifications == tuple(sorted(fixtures.PAYLOADS))


def test_a_placeholder_left_by_an_interrupted_upload_is_discarded() -> None:
    """An empty record is a name reservation, not an asset with wrong bytes.

    GitHub refuses an upload under a name it already lists, so the empty
    record has to be deleted before the file can be sent. Reporting its size
    as a mismatch would turn a recoverable upload into a permanent conflict.
    """
    name = "backend.tar.gz"
    assets = tuple(
        fixtures.placeholder(name) if asset.name == name else asset
        for asset in fixtures.verified_assets()
    )
    decision = decide(
        fixtures.expected(), fixtures.draft(assets), fixtures.validation()
    )
    assert decision.action is ReleaseAction.DISCARD_PLACEHOLDER
    assert [asset.name for asset in decision.discards] == [name]


def test_a_placeholder_beside_a_mismatched_asset_is_a_conflict() -> None:
    """A conflicting draft is refused before any of its records are deleted.

    D5 keeps the bytes of a conflicting draft for diagnosis, so a discard is
    only authorised by a draft that is otherwise sound. The placeholder alone
    would be recoverable; the mismatched asset beside it is not, and it must
    be reported first.
    """
    reserved = "backend.tar.gz"
    wrong = "launcher.tar.gz"
    assets = tuple(
        fixtures.placeholder(reserved)
        if asset.name == reserved
        else fixtures.observed_asset(wrong, sha256="f" * 64)
        if asset.name == wrong
        else asset
        for asset in fixtures.verified_assets()
    )
    decision = decide(
        fixtures.expected(), fixtures.draft(assets), fixtures.validation()
    )
    assert decision.action is ReleaseAction.CONFLICT
    assert wrong in decision.reason
    assert decision.discards == ()
    assert decision.uploads == ()


def test_a_complete_verified_draft_is_published() -> None:
    """Only a complete, hashed draft may become public."""
    decision = decide(
        fixtures.expected(),
        fixtures.draft(fixtures.verified_assets()),
        fixtures.validation(),
    )
    assert decision.action is ReleaseAction.PUBLISH


def test_a_complete_draft_whose_evidence_does_not_verify_conflicts() -> None:
    """The draft's own evidence authorises publication, before it is public.

    A bundle whose manifest, checksums or attestation do not agree can still be
    repaired while the release is a draft. Publishing first and discovering the
    disagreement afterwards would leave a public release nobody can change.
    """
    observed = fixtures.draft(
        fixtures.verified_assets(),
        provenance_verified=False,
        provenance_reason="attestation is not signed by the release workflow",
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "did not authenticate its manifest" in decision.reason
    assert "not signed by the release workflow" in decision.reason


def test_a_complete_draft_not_yet_verified_asks_for_verification() -> None:
    """An observation that has not downloaded bytes cannot authorise a publish.

    Metadata that happens to carry digests is still a claim, so the next step
    is verification rather than publication or a conflict.
    """
    observed = fixtures.draft(fixtures.verified_assets(), provenance_verified=False)
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.VERIFY_ASSETS
    assert decision.verifications == tuple(sorted(fixtures.PAYLOADS))


def test_a_draft_with_a_foreign_asset_conflicts() -> None:
    """An unexplained asset is never deleted to make publication possible."""
    extra = (*fixtures.verified_assets(), ObservedAsset("surprise.bin", 1, "e" * 64))
    decision = decide(fixtures.expected(), fixtures.draft(extra), fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "outside the expected set" in decision.reason


def test_a_draft_with_the_wrong_size_conflicts() -> None:
    """A same-named asset of another size is a conflict, not an upload."""
    name = "backend.tar.gz"
    wrong = fixtures.assets_with_size(name, len(fixtures.PAYLOADS[name]) + 1)
    decision = decide(fixtures.expected(), fixtures.draft(wrong), fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "differ from the expected bytes" in decision.reason


def test_a_draft_with_other_bytes_conflicts() -> None:
    """A same-size asset with other bytes is still a conflict."""
    wrong = fixtures.assets_with_digest("backend.tar.gz", fixtures.digest(b"other"))
    decision = decide(fixtures.expected(), fixtures.draft(wrong), fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "differ from the expected bytes" in decision.reason


def test_a_draft_that_is_wrong_and_incomplete_conflicts_before_uploading() -> None:
    """A present asset with other bytes outranks an absent one.

    A draft that carries the wrong bytes can never be completed, so the
    conflict must be diagnosed before an upload for a missing asset is
    requested; otherwise the run would write to a draft it has to abandon.
    """
    wrong = fixtures.assets_with_digest("backend.tar.gz", fixtures.digest(b"other"))
    decision = decide(fixtures.expected(), fixtures.draft(wrong), fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "differ from the expected bytes" in decision.reason
    assert not decision.uploads


def test_a_draft_for_another_tag_conflicts() -> None:
    """A draft under the expected tag that reports another one is ambiguous."""
    observed = dc.replace(
        fixtures.draft(fixtures.verified_assets()), tag="kani-snapshot-x"
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "another tag" in decision.reason


def test_a_draft_targeting_another_commit_conflicts() -> None:
    """The immutable tag must name the recorded builder commit."""
    observed = dc.replace(
        fixtures.draft(fixtures.verified_assets()), target_commitish="f" * 40
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "builder commit" in decision.reason


def test_a_published_release_matching_exactly_is_a_no_op() -> None:
    """Recovery from a lost response ends in observation, not a second write."""
    observed = fixtures.published(fixtures.verified_assets())
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.NOOP


def test_a_published_release_with_unverified_provenance_conflicts() -> None:
    """A published release whose bytes did not re-verify is never a no-op."""
    observed = fixtures.published(
        fixtures.verified_assets(),
        provenance_verified=False,
        provenance_reason="attestation does not cover the published manifest",
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "did not authenticate its own publication" in decision.reason
    assert "does not cover the published manifest" in decision.reason


def test_a_published_release_not_yet_verified_asks_for_verification() -> None:
    """A metadata-only observation must be verified before it is a no-op.

    Otherwise "already published" would be a claim read from the API rather
    than evidence computed from the bytes the release serves.
    """
    observed = fixtures.published(fixtures.verified_assets(), provenance_verified=False)
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.VERIFY_ASSETS
    assert decision.verifications == tuple(sorted(fixtures.PAYLOADS))


def test_a_published_release_with_a_different_asset_set_conflicts() -> None:
    """An asset set that changed after publication is never repaired in place."""
    observed = fixtures.published(fixtures.verified_assets()[:-1])
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "different asset set" in decision.reason


def test_a_published_release_recording_other_inputs_conflicts() -> None:
    """A newer input set takes a new revision, never this release."""
    observed = fixtures.published(
        fixtures.verified_assets(),
        body_text=fixtures.body(fingerprint=fixtures.OTHER_FINGERPRINT),
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "different build inputs" in decision.reason


def test_a_published_release_without_recorded_inputs_conflicts() -> None:
    """A release that cannot name its inputs is not assumed to match."""
    observed = fixtures.published(fixtures.verified_assets(), body_text=None)
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "does not record its build inputs" in decision.reason


def test_a_draft_from_another_attempt_is_never_resumed() -> None:
    """Only the attempt holding the validation evidence may finish a draft."""
    observed = fixtures.draft(
        fixtures.verified_assets(), body_text=fixtures.body(run_attempt=2)
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "another workflow run or attempt" in decision.reason


def test_a_draft_from_other_inputs_is_never_resumed() -> None:
    """A draft built from other inputs belongs to a different revision."""
    observed = fixtures.draft(
        fixtures.verified_assets(),
        body_text=fixtures.body(fingerprint=fixtures.OTHER_FINGERPRINT),
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "different build inputs" in decision.reason


def test_a_draft_without_recorded_origin_conflicts() -> None:
    """A draft whose creator cannot be identified is not adoptable."""
    observed = fixtures.draft(fixtures.verified_assets(), body_text=None)
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "does not record the run" in decision.reason
