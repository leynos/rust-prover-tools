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
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish import (
    PublicationRequest,
    publish_release,
)
from rust_prover_tools.kani_snapshot.release_policy import ReleaseAction
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
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
    api = fixtures.FakeReleaseApi()
    api.place_published()
    api.stored = {name: data + b"\nrebuilt" for name, data in fixtures.PAYLOADS.items()}
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 0
    assert not api.uploads, "an unchanged input set uploaded assets again"


def test_a_draft_from_another_attempt_is_refused_before_any_write(
    tmp_path: pathlib.Path,
) -> None:
    """Draft recovery is bound to the run and attempt that created it."""
    api = fixtures.FakeReleaseApi()
    api.place_draft()
    api.body_text = fixtures.body(run_attempt=2)
    with pytest.raises(ProverToolError, match="another workflow run or attempt"):
        publish_release(_request(tmp_path), api)
    assert api.calls == ["observe"]
    assert not api.uploads


def test_publication_uploads_then_publishes_in_order(tmp_path: pathlib.Path) -> None:
    """The full transition is observe, create, upload, publish, observe."""
    api = fixtures.FakeReleaseApi()
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.is_draft is False
    assert api.stored == fixtures.PAYLOADS
    assert api.uploads == sorted(fixtures.PAYLOADS)
    assert api.calls.count("publish") == 1


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
    api = fixtures.FakeReleaseApi(digests_visible=digests_visible)
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
    api = fixtures.FakeReleaseApi(digests_visible=False)
    api.fail_next("verify_assets", after=after)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("verify_assets") >= 2


def test_a_lost_publish_response_is_recognised_as_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A timed-out publish that succeeded must not be retried blindly."""
    api = fixtures.FakeReleaseApi()
    api.fail_next("publish", after=True)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 1


def test_publication_verifies_unhashed_assets_before_publishing(
    tmp_path: pathlib.Path,
) -> None:
    """With no API digests, publication downloads and hashes every asset."""
    api = fixtures.FakeReleaseApi(digests_visible=False)
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("verify_assets") >= 1
    assert api.is_draft is False


def test_a_local_asset_mismatch_stops_before_any_api_call(
    tmp_path: pathlib.Path,
) -> None:
    """A tampered local payload never reaches the release API."""
    request = _request(tmp_path)
    request.asset_paths["backend.tar.gz"].write_bytes(b"tampered")
    api = fixtures.FakeReleaseApi()
    with pytest.raises(ProverToolError, match="does not match its manifest"):
        publish_release(request, api)
    assert not api.calls


def test_a_cross_run_receipt_stops_before_any_effect(
    tmp_path: pathlib.Path,
) -> None:
    """Accepting another attempt's receipt would publish unvalidated bytes."""
    api = fixtures.FakeReleaseApi()
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
    api = fixtures.FakeReleaseApi()
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
    api = fixtures.FakeReleaseApi()
    for _ in range(4):
        api.fail_next("observe")
    with pytest.raises(ProverToolError, match="did not converge"):
        publish_release(_request(tmp_path), api, max_steps=4)
