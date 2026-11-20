"""Publication is a guarded state machine, not a sequence of writes.

These tests drive the real orchestration over a deterministic release API that
injects a transient failure before or after each operation, including the
dangerous case where a publication call succeeds but its response is lost. The
lemma they hold is that no failure can make the publisher upload twice, publish
unverified bytes, or replace anything already published: recovery is always a
fresh observation.

The pure decision rules are witnessed in `test_kani_snapshot_release_policy`,
the generated state machine lives in `test_kani_snapshot_publish_machine`, and
the `gh` adapter's argument vectors are checked in `test_kani_gh_release_api`.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish import (
    PublicationRequest,
    publish_release,
)
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseAction,
    VerifiedAssets,
)
from tests import kani_publish_fixtures as fixtures
from tests.kani_release_api_fixtures import FakeReleaseApi

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from rust_prover_tools.kani_snapshot.release_policy import ValidationBinding


def _request(
    tmp_path: pathlib.Path,
    validation: ValidationBinding | None = None,
) -> PublicationRequest:
    """Return a publication request whose local assets are exact."""
    return fixtures.request(tmp_path, validation)


def test_an_unchanged_input_set_is_a_no_op_for_the_published_release(
    tmp_path: pathlib.Path,
) -> None:
    """A rebuild produces a new manifest, so only the fingerprint can match.

    The published release's bytes are its own: the fake reports them verified
    against its own manifest. This run's locally assembled manifest has a
    different digest, and that must not turn an unchanged input set into a
    conflict or a second publication.
    """
    api = FakeReleaseApi()
    api.place_published()
    api.stored = {name: data + b"\nrebuilt" for name, data in fixtures.PAYLOADS.items()}
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 0
    assert not api.uploads, "an unchanged input set uploaded assets again"


class _NotesRewrittenDuringVerifyApi(FakeReleaseApi):
    """Rewrite a published release's notes while its bytes are downloaded.

    Release notes stay editable after publication, so the input fingerprint
    they carry can be removed during a verification that started before the
    edit. The verdict describes the release as it was, not as it is.
    """

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Verify the release, then remove the inputs its notes recorded."""
        verified = super().verify(tag, observed, names)
        self.body_text = "notes rewritten while the release was being read"
        return verified


def test_rewriting_published_notes_during_verification_is_not_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A verification must not outlive the notes it authenticated.

    The release matches the expected inputs, so verification is requested
    before "already published" is reported. During the download the notes lose
    their input marker: the release is no longer the one this run built, and a
    no-op would hide that. The confirming read must discard the verdict and
    report the conflict instead.
    """
    api = _NotesRewrittenDuringVerifyApi()
    api.place_published()
    api.provenance_verified = False

    with pytest.raises(ProverToolError, match="does not record its build inputs"):
        publish_release(_request(tmp_path), api)

    assert "verify" in api.calls, "the release was never verified, so nothing tested"
    assert api.calls.count("publish") == 0


def test_a_draft_from_another_attempt_is_refused_before_any_write(
    tmp_path: pathlib.Path,
) -> None:
    """Draft recovery is bound to the run and attempt that created it."""
    api = FakeReleaseApi()
    api.place_draft()
    api.body_text = fixtures.body(run_attempt=2)
    with pytest.raises(ProverToolError, match="another workflow run or attempt"):
        publish_release(_request(tmp_path), api)
    assert api.calls == ["observe"]
    assert not api.uploads


def test_publication_uploads_then_publishes_in_order(tmp_path: pathlib.Path) -> None:
    """The full transition is observe, create, upload, publish, observe."""
    api = FakeReleaseApi()
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.is_draft is False
    assert api.stored == fixtures.PAYLOADS
    assert api.uploads == sorted(fixtures.PAYLOADS)
    assert api.calls.count("publish") == 1


def test_a_placeholder_left_by_an_interrupted_upload_is_replaced(
    tmp_path: pathlib.Path,
) -> None:
    """A draft with an empty record for one asset still reaches publication.

    The record is what a failed upload leaves behind: it reserves the name but
    holds no bytes. Publication deletes it, uploads the missing file and then
    publishes, instead of ending the run in a conflict it could have repaired.
    """
    request = _request(tmp_path)
    api = FakeReleaseApi()
    api.place_draft()
    for name in ("launcher.tar.gz", "notices.tar.gz", "inventory.json"):
        api.place_asset(name, request.asset_paths[name])
    api.place_placeholder("backend.tar.gz")

    decision = publish_release(request, api)

    assert decision.action is ReleaseAction.NOOP, (
        "the interrupted draft did not recover"
    )
    assert api.discards == ["backend.tar.gz"], "the empty record was not deleted"
    assert api.uploads == ["backend.tar.gz"], "only the missing asset was uploaded"
    assert api.stored == fixtures.PAYLOADS


@pytest.mark.parametrize("digests_visible", [True, False], ids=["hashed", "unhashed"])
@pytest.mark.parametrize("operation", ["observe", "create_draft", "upload", "publish"])
@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
def test_a_transient_failure_never_duplicates_publication(
    tmp_path: pathlib.Path,
    operation: str,
    *,
    after: bool,
    digests_visible: bool,
) -> None:
    """Every operation is recoverable by re-observing rather than repeating."""
    api = FakeReleaseApi(digests_visible=digests_visible)
    api.fail_next(operation, after=after)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.stored == fixtures.PAYLOADS
    assert api.uploads == sorted(fixtures.PAYLOADS), "an upload was repeated"
    # A publish that failed before landing changed nothing, so retrying it is
    # the only way to converge; a publish that may have landed is re-observed
    # instead and must never be sent twice.
    expected_publishes = 2 if (operation, after) == ("publish", False) else 1
    assert api.calls.count("publish") == expected_publishes


@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
def test_a_transient_verification_failure_is_retried(
    tmp_path: pathlib.Path, *, after: bool
) -> None:
    """Verification is the one step safe to repeat, and must be repeated."""
    api = FakeReleaseApi(digests_visible=False)
    api.fail_next("verify", after=after)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("verify") >= 2


class _AssetMissingBeforeVerificationApi(FakeReleaseApi):
    """A release whose asset disappears before the download that would verify it.

    The removal happens after the fake captures a complete-draft observation
    but before `verify` downloads those bytes, so the observation still lists
    the asset while the download finds it gone. `verify` reports that as the
    transport failure it is, and the publisher re-observes and uploads the
    missing file rather than trusting a reading it could not complete.
    """

    def __init__(self) -> None:
        """Create the fake with the removal still pending."""
        super().__init__()
        self._removed = False

    def observe(self, tag: str) -> ObservedRelease:
        """Observe the release, removing one asset once a draft is complete."""
        observed = super().observe(tag)
        if not self._removed and set(self.stored) == set(fixtures.PAYLOADS):
            self._removed = True
            del self.stored["backend.tar.gz"]
        return observed


def test_an_asset_missing_before_its_download_is_uploaded_again(
    tmp_path: pathlib.Path,
) -> None:
    """A missing download is a transport failure, and the draft is repaired.

    A maintainer can remove an asset between the observation that listed a
    complete draft and the download that would have verified it. The download
    cannot complete, which the adapter reports as the transient failure it is,
    so the publisher observes again, finds the asset gone, and uploads it
    rather than publishing a release that is missing a file. Removal *during*
    the publication recheck is covered separately in
    `tests/test_kani_snapshot_publish_race.py`.
    """
    api = _AssetMissingBeforeVerificationApi()
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.stored == fixtures.PAYLOADS
    assert api.uploads.count("backend.tar.gz") == 2, (
        "the asset missing before its download was not uploaded again"
    )
    assert api.calls.count("publish") == 1, "publication was attempted twice"


def test_a_lost_publish_response_is_recognised_as_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A timed-out publish that succeeded must not be retried blindly."""
    api = FakeReleaseApi()
    api.fail_next("publish", after=True)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 1


def test_publication_verifies_unhashed_assets_before_publishing(
    tmp_path: pathlib.Path,
) -> None:
    """With no API digests, publication downloads and hashes every asset."""
    api = FakeReleaseApi(digests_visible=False)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("verify") >= 1
    assert api.is_draft is False


def test_a_local_asset_mismatch_stops_before_any_api_call(
    tmp_path: pathlib.Path,
) -> None:
    """A tampered local payload never reaches the release API."""
    request = _request(tmp_path)
    request.asset_paths["backend.tar.gz"].write_bytes(b"tampered")
    api = FakeReleaseApi()
    with pytest.raises(ProverToolError, match="does not match its manifest"):
        publish_release(request, api)
    assert not api.calls


def test_a_cross_run_receipt_stops_before_any_effect(
    tmp_path: pathlib.Path,
) -> None:
    """Accepting another attempt's receipt would publish unvalidated bytes."""
    api = FakeReleaseApi()
    stale = dc.replace(fixtures.validation(), run_attempt=2)
    with pytest.raises(ProverToolError, match="publication conflict"):
        publish_release(_request(tmp_path, stale), api)
    # Reading state is not an effect; no release was created or changed.
    assert api.calls == ["observe"]
    assert not api.uploads


def test_publication_refuses_a_published_release_with_other_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """A conflicting published release raises rather than replacing assets."""
    api = FakeReleaseApi()
    api.exists = True
    api.is_draft = False
    api.stored = dict.fromkeys(fixtures.PAYLOADS, b"other")
    with pytest.raises(ProverToolError, match="publication conflict"):
        publish_release(_request(tmp_path), api)
    assert api.calls.count("publish") == 0
    assert not api.uploads


def test_a_persistently_failing_observation_never_converges(
    tmp_path: pathlib.Path,
) -> None:
    """An API that never answers is refused rather than treated as success."""
    api = FakeReleaseApi()
    for _ in range(4):
        api.fail_next("observe")
    with pytest.raises(ProverToolError, match="did not converge"):
        publish_release(_request(tmp_path), api, max_steps=4)


_LOGGER_NAME: typ.Final[str] = "rust_prover_tools.kani_snapshot.publish"


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured publication events captured by `caplog`."""
    return [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]


def test_a_publication_reports_each_decision_and_its_outcome(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log is a bounded trace of the state machine, not of its payloads."""
    api = FakeReleaseApi()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        publish_release(_request(tmp_path), api)

    events = _events(caplog)
    decisions = [
        event for event in events if event["event"] == "kani.snapshot.publish.decision"
    ]
    assert [(event["step"], event["action"]) for event in decisions] == [
        (1, "create-draft"),
        (2, "upload-assets"),
        (3, "verify-assets"),
        (4, "publish"),
        (5, "verify-assets"),
        (6, "noop"),
    ]
    assert all(set(event) == {"event", "step", "action"} for event in decisions)
    assert events[-1]["event"] == "kani.snapshot.publish.complete"
    assert events[-1]["outcome"] == "noop"


def test_a_transient_effect_is_reported_by_the_action_it_retries(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retryable write is visible as such, without its error text."""
    api = FakeReleaseApi()
    api.fail_next("publish")
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        publish_release(_request(tmp_path), api)

    transient = [
        event
        for event in _events(caplog)
        if event["event"] == "kani.snapshot.publish.transient"
    ]
    assert transient == [
        {"event": "kani.snapshot.publish.transient", "action": "publish"}
    ]


def test_a_conflict_closes_the_span_with_the_error_class(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure is recorded by class name; its message may embed paths."""
    api = FakeReleaseApi()
    api.place_published(fingerprint=fixtures.OTHER_FINGERPRINT)
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME),
        pytest.raises(ProverToolError, match="publication conflict"),
    ):
        publish_release(_request(tmp_path), api)

    failed = _events(caplog)[-1]
    assert failed["event"] == "kani.snapshot.publish.failed"
    assert failed["reason"] == "ProverToolError"
