"""Generated publication histories never publish unverified or replace bytes.

A single publication is easy to reason about; an interrupted one is not. This
machine composes the states an earlier attempt can leave behind — absent, draft,
complete, published, and conflicting — with transient failures on either side of
every API operation, and then runs the real orchestration. The invariants are
the publication lemma stated over histories rather than over one decision.

The named witnesses beside the machine pin the transitions a reader should be
able to follow without replaying a generated example: absent to published, a
foreign draft asset to conflict, a published release to no-op, and a cross-run
receipt to refusal before any effect.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish import publish_release
from rust_prover_tools.kani_snapshot.release_policy import (
    ReleaseAction,
    ReleaseTransientError,
)
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
    import pathlib

    from rust_prover_tools.kani_snapshot.publish import PublicationRequest
    from rust_prover_tools.kani_snapshot.release_policy import ValidationBinding

_MACHINE_SETTINGS = settings(
    max_examples=6,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


class PublicationMachine(RuleBasedStateMachine):
    """Generated absent, draft, complete, published and conflict transitions."""

    def __init__(self, root: pathlib.Path) -> None:
        """Create one isolated request and its fake API."""
        super().__init__()
        self.request = fixtures.request(root)
        self.api = fixtures.FakeReleaseApi(digests_visible=True)
        self.conflicted = False
        self.completed = False

    @rule()
    def create_draft(self) -> None:
        """Create the draft as an earlier, interrupted attempt would have."""
        if not self.api.exists:
            self.api.place_draft()

    @rule(name=st.sampled_from(sorted(fixtures.PAYLOADS)))
    @precondition(lambda self: self.api.is_draft)
    def upload(self, name: str) -> None:
        """Place one expected asset as an earlier, interrupted upload would."""
        self.api.place_asset(name, self.request.asset_paths[name])

    @rule(name=st.sampled_from(sorted(fixtures.PAYLOADS)))
    @precondition(lambda self: self.api.is_draft)
    def drop(self, name: str) -> None:
        """Let an asset disappear, as an interrupted upload might."""
        self.api.stored.pop(name, None)

    @rule(name=st.sampled_from(sorted(fixtures.PAYLOADS)))
    def corrupt(self, name: str) -> None:
        """Replace stored bytes with something the manifest did not name."""
        # Hypothesis preconditions cannot read a rule's arguments, so the
        # guard lives here; a published release is never rewritten.
        if self.api.is_draft and name in self.api.stored:
            self.api.stored[name] = b"corrupt"

    @rule(failure=st.sampled_from(fixtures.FAILURE_POINTS))
    def inject_failure(self, failure: str) -> None:
        """Schedule one transient failure around the next effect."""
        operation, when = failure.split(":")
        self.api.fail_next(operation, after=when == "after")

    @rule()
    def drive_publication(self) -> None:
        """Run the real orchestration and record its verdict."""
        try:
            decision = publish_release(self.request, self.api)
        except ReleaseTransientError:
            raise
        except ProverToolError:
            self.conflicted = True
            return
        assert decision.action is ReleaseAction.NOOP, (
            f"publication stopped at {decision.action}"
        )
        self.completed = True

    @invariant()
    def publish_only_sees_the_exact_asset_set(self) -> None:
        """Require a completed publication to have stored every asset."""
        if not self.api.is_draft and self.completed:
            assert self.api.stored == fixtures.PAYLOADS

    @invariant()
    def a_published_release_keeps_its_bytes(self) -> None:
        """Once published, every stored asset still matches the manifest."""
        if self.api.exists and not self.api.is_draft:
            assert self.api.stored == fixtures.PAYLOADS
            # A publish retried after failing before it landed is legitimate;
            # a second publish of a published release raises in the adapter.
            assert self.api.calls.count("publish") >= 1

    @invariant()
    def no_asset_outside_the_expected_set_is_stored(self) -> None:
        """Keep every stored asset name inside the expected set."""
        assert set(self.api.stored) <= set(fixtures.PAYLOADS)


def _machine_factory(root: pathlib.Path) -> type[RuleBasedStateMachine]:
    """Return a zero-argument factory for a machine rooted under `root`.

    Hypothesis documents that the factory may be any callable taking no
    arguments, but annotates the parameter as a class. The cast records that
    upstream annotation mismatch instead of hiding it behind an ignore.
    """

    def build() -> PublicationMachine:
        """Create the machine with its own payload directory."""
        return PublicationMachine(root)

    return typ.cast("type[RuleBasedStateMachine]", build)


def test_publication_converges_or_conflicts_over_generated_transitions(
    tmp_path: pathlib.Path,
) -> None:
    """Thirty generated operations never publish unverified or replace bytes."""
    run_state_machine_as_test(_machine_factory(tmp_path), settings=_MACHINE_SETTINGS)


def test_witness_absent_becomes_a_published_exact_release(
    tmp_path: pathlib.Path,
) -> None:
    """Named witness: absent, draft, complete, published, no-op."""
    api = fixtures.FakeReleaseApi()
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.exists
    assert not api.is_draft


def test_witness_a_foreign_draft_asset_conflicts(tmp_path: pathlib.Path) -> None:
    """Named witness: a draft carrying an unknown asset is refused."""
    api = fixtures.FakeReleaseApi()
    api.create_draft(fixtures.expected())
    api.stored["surprise.bin"] = b"unexpected"
    with pytest.raises(ProverToolError, match="outside the expected set"):
        publish_release(_request(tmp_path), api)


def test_witness_a_published_release_is_not_republished(
    tmp_path: pathlib.Path,
) -> None:
    """Named witness: a matching published release is a no-op."""
    api = fixtures.FakeReleaseApi()
    api.place_published()
    decision = publish_release(_request(tmp_path), api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("publish") == 0


def test_witness_a_cross_run_receipt_is_refused(tmp_path: pathlib.Path) -> None:
    """Named witness: a receipt from another attempt is a conflict."""
    api = fixtures.FakeReleaseApi()
    stale = dc.replace(fixtures.validation(), run_id=fixtures.RUN_ID + 1)
    with pytest.raises(ProverToolError, match="publication conflict"):
        publish_release(_request(tmp_path, stale), api)
    assert api.calls == ["observe"]
    assert not api.uploads


def _request(
    tmp_path: pathlib.Path,
    validation_binding: ValidationBinding | None = None,
) -> PublicationRequest:
    """Return a publication request whose local assets are exact."""
    return fixtures.request(tmp_path, validation_binding)
