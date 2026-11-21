"""A published release is a no-op only when its own evidence verifies.

A published release cannot be edited, so the policy's only safe outcomes for
one are to accept it as already published or to refuse it. Accepting it is a
claim about the release's own bytes, so the API's metadata is not evidence: the
release has to authenticate itself against its own manifest first, and its
recorded inputs have to be the ones this run would publish. Everything else —
another asset set, another build, a release that did not verify, or one whose
tag ref does not resolve — is a conflict, because none of it is repaired in
place.
"""

from __future__ import annotations

import dataclasses as dc

from rust_prover_tools.kani_snapshot.release_policy import (
    ReleaseAction,
    decide,
)
from tests import kani_publish_fixtures as fixtures


def test_a_published_release_without_a_tag_is_refused() -> None:
    """GitHub creates the tag on publication, so a missing one means no target."""
    observed = dc.replace(
        fixtures.published(fixtures.verified_assets()),
        tag_commit=None,
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "does not resolve to a tag ref" in decision.reason


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


def test_published_metadata_is_refused_before_any_download_is_requested() -> None:
    """A release the notes already refuse is never asked for its bytes.

    The assets carry no digest, so the download that would authenticate them
    is the expensive step. When the recorded inputs differ, that download
    cannot change the verdict, and asking for it would let a transport failure
    be reported in place of the conflict the metadata already establishes.
    """
    unverified = tuple(
        fixtures.observed_asset(name, sha256=None) for name in fixtures.PAYLOADS
    )
    observed = fixtures.published(
        unverified,
        body_text=fixtures.body(fingerprint=fixtures.OTHER_FINGERPRINT),
    )
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "different build inputs" in decision.reason
    assert decision.verifications == (), "a refused release was still downloaded"


def test_a_published_release_without_recorded_inputs_conflicts() -> None:
    """A release that cannot name its inputs is not assumed to match."""
    observed = fixtures.published(fixtures.verified_assets(), body_text=None)
    decision = decide(fixtures.expected(), observed, fixtures.validation())
    assert decision.action is ReleaseAction.CONFLICT
    assert "does not record its build inputs" in decision.reason
