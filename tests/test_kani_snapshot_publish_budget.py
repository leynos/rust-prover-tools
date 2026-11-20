"""The publication's step budget, and the observation that closes it.

A publish is the one irreversible write a publication makes, so a budget that
runs out on that step cannot simply report that the state never converged: the
release may already be public. These tests hold the three endings the loop can
reach. A budget that ends on the publish call is confirmed once more, then
reported as unconfirmed rather than repeated — or as the conflict the
confirmation observed. A budget that ends on the recheck which withdrew the
write reports a state that did not converge, because nothing was published. A
confirmation that cannot be read never denies the write it could not see.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish import publish_release
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseAction,
    ReleaseTransientError,
    VerifiedAssets,
)
from tests import kani_publish_fixtures as fixtures
from tests.kani_release_api_fixtures import FakeReleaseApi

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib


def test_a_persistently_failing_observation_never_converges(
    tmp_path: pathlib.Path,
) -> None:
    """An API that never answers is refused rather than treated as success."""
    api = FakeReleaseApi()
    for _ in range(4):
        api.fail_next("observe")
    with pytest.raises(ProverToolError, match="did not converge"):
        publish_release(fixtures.request(tmp_path), api, max_steps=4)


def test_a_budget_that_exactly_covers_the_transition_converges(
    tmp_path: pathlib.Path,
) -> None:
    """Every step the transition needs must be affordable.

    A fresh publication is create, upload, verify, publish, confirm and no-op:
    six observed steps. A budget of exactly six must succeed, which fails if
    the loop bound spends one of them on its own counting or if any step is
    performed twice.
    """
    api = FakeReleaseApi()
    decision = publish_release(fixtures.request(tmp_path), api, max_steps=6)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 1


def test_a_publish_on_the_last_allowed_step_is_not_reported_as_converged(
    tmp_path: pathlib.Path,
) -> None:
    """A write that landed is confirmed, not described as a state that never moved.

    With four steps the budget ends immediately after the publish call. The
    publication has landed, so the error must say so: reporting that the state
    "did not converge" would invite a retry that repeats work already done.
    """
    api = FakeReleaseApi()
    with pytest.raises(ProverToolError, match="may already be public") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert "did not converge" not in str(raised.value), (
        "a landed publish was reported as a state that never converged"
    )
    assert api.is_draft is False, "the publish under test did not land"
    assert api.calls.count("publish") == 1, "publication was attempted twice"
    assert api.calls[-2:] == ["publish", "observe"], (
        "the write was not confirmed by an observation taken after it"
    )


class _LostPublishOnTheLastStepApi(FakeReleaseApi):
    """Report a publish that landed as a transient failure.

    The response is lost after the write took effect, which is the case the
    publisher must never resolve by writing again.
    """

    def __init__(self) -> None:
        """Create the fake with its single publish failure armed."""
        super().__init__()
        self.fail_next("publish", after=True)


def test_a_lost_publish_response_on_the_last_step_is_not_a_second_write(
    tmp_path: pathlib.Path,
) -> None:
    """An ambiguous final publish is observed once, never repeated."""
    api = _LostPublishOnTheLastStepApi()
    with pytest.raises(ProverToolError, match="may already be public"):
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert api.calls.count("publish") == 1, "an ambiguous publish was sent twice"
    assert api.is_draft is False, "the publish under test did not land"
    assert api.calls[-2:] == ["publish", "observe"], (
        "the ambiguous write was not observed after its failure"
    )


class _UnconfirmablePublishApi(FakeReleaseApi):
    """Refuse every observation taken after the publish call has landed."""

    def __init__(self) -> None:
        """Create the fake with its release still readable."""
        super().__init__()
        self.published = False
        self.confirmations = 0

    def publish(self, tag: str, release_id: int) -> None:
        """Publish, then make the release unreadable from that moment on."""
        super().publish(tag, release_id)
        self.published = True

    def observe(self, tag: str) -> ObservedRelease:
        """Fail the confirmation read, as an unreachable API would."""
        if self.published:
            self.confirmations += 1
            raise ReleaseTransientError(tag)
        return super().observe(tag)


def test_an_unreadable_confirmation_still_reports_the_landed_write(
    tmp_path: pathlib.Path,
) -> None:
    """A budget ending on a publish stays honest when it cannot confirm it.

    The release cannot be read after the write, so nothing can say whether it
    verified. The failure must still say the publish may have landed, because
    the one outcome this run cannot claim is that nothing happened.
    """
    api = _UnconfirmablePublishApi()
    with pytest.raises(ProverToolError, match="may already be public"):
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert api.calls.count("publish") == 1
    assert api.confirmations == 1, "the landed write was never read after the fact"


class _PublishRewritesTheReleaseApi(FakeReleaseApi):
    """Publish, then serve notes recording a different build.

    The write landed and the confirmation finds a release this run would
    refuse. That is a diagnosis about a public release rather than an unknown
    outcome, and it names the divergence instead of hiding it behind "may have
    landed".
    """

    def publish(self, tag: str, release_id: int) -> None:
        """Publish, then rewrite the notes the confirmation will read."""
        super().publish(tag, release_id)
        self.body_text = fixtures.body(fingerprint=fixtures.OTHER_FINGERPRINT)


def test_a_final_publish_that_published_other_inputs_reports_the_conflict(
    tmp_path: pathlib.Path,
) -> None:
    """A divergence the confirmation observes is reported with its reason."""
    api = _PublishRewritesTheReleaseApi()
    with pytest.raises(ProverToolError, match="publication conflict") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert "different build inputs" in str(raised.value)
    assert "may already be public" not in str(raised.value), (
        "an observed divergence was reported as an unknown outcome"
    )
    assert api.calls.count("publish") == 1
    assert api.calls[-2:] == ["publish", "observe"], (
        "the conflict was not read from an observation taken after the write"
    )


class _RecheckWithdrawsTheWriteApi(FakeReleaseApi):
    """Remove an asset while the pre-publish recheck downloads the draft.

    The recheck reads, downloads, and reads again. Removing an asset between
    those readings is the case it exists for: the state that authorised the
    write is no longer the state the write would be made against.
    """

    def __init__(self) -> None:
        """Create the fake with its second verification still ahead."""
        super().__init__()
        self.verifications = 0

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Delete one asset as soon as the recheck has downloaded the draft."""
        verified = super().verify(tag, observed, names)
        self.verifications += 1
        if self.verifications == 2:
            del self.stored["backend.tar.gz"]
        return verified


def test_a_recheck_that_stops_the_final_publish_does_not_claim_a_write(
    tmp_path: pathlib.Path,
) -> None:
    """The step that ends the budget can be the one that refused to write.

    The recheck withdraws authority for the publish on the last allowed step,
    so nothing was published. The run must not report that a write may have
    landed, because there is no write to confirm; the reading that withdrew it
    is the state a later run continues from.
    """
    api = _RecheckWithdrawsTheWriteApi()
    with pytest.raises(ProverToolError, match="did not converge") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert "may already be public" not in str(raised.value), (
        "a write the recheck stopped was reported as one that may have landed"
    )
    assert api.calls.count("publish") == 0, "the withdrawn write was attempted"
    assert api.is_draft is True, "the release was published under test"
