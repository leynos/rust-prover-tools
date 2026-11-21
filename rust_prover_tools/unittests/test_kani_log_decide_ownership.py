"""Prove a block's evidence is read against the routine that owns it.

A log reports checks and covers without saying which harness each belongs to,
so ownership is inferred from the name: a property named ``h::c.assertion.1``
belongs to ``h::c``, while ``other::helper.cover.1`` belongs to the routine
CBMC reported it under.  Getting that wrong credits one harness with another's
witness, or with a failure that routine never attributed to it, and the run is
decided on evidence it does not have.
"""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log import (
    CLAIM,
    Harness,
    KaniOutcome,
    Manifest,
    PropertyResult,
    analyse_kani_log,
)
from tests import kani_log_decide_fixtures as shapes
from tests import kani_log_fixtures as facts


def test_a_cover_is_matched_to_the_claim_that_declares_it() -> None:
    """Require two claims sharing witness wording to keep their own results."""
    combination = (
        facts.HarnessFact(
            "h::a",
            CLAIM,
            facts.SUCCESS,
            facts.SATISFIED,
            cover_description="the witness",
        ),
        facts.HarnessFact(
            "h::b",
            CLAIM,
            facts.ABSENT,
            facts.SATISFIED,
            cover_description="the witness",
        ),
    )
    decision = facts.decision_for(combination)
    assert decision.witness_statuses == ("satisfied", "unsatisfied")
    assert decision.outcome == KaniOutcome.SKIPPED


def test_a_foreign_cover_does_not_satisfy_a_declared_witness() -> None:
    """Refuse a called routine's cover as this claim's witness.

    Two harnesses may describe their witnesses with the same words, so a cover
    owned by another routine says that routine's cover was reached, not this
    claim's.  Only an ownerless cover is read from the block it appears in.
    """
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other::helper.cover.1", "SATISFIED", "the witness"),
        failed_count=0,
        total_count=2,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, covers=("the witness",)),))
    decision = analyse_kani_log(log, manifest)
    assert decision.witness_statuses == ("unsatisfied",)


def test_an_ownerless_cover_is_matched_to_its_own_block() -> None:
    """Count a cover CBMC left unattributed as its own block's witness."""
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult("cover.1", "SATISFIED", "the witness"),
        failed_count=0,
        total_count=0,
        covered_satisfied=1,
        covered_total=1,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, covers=("the witness",)),))
    decision = analyse_kani_log(log, manifest)
    assert decision.witness_statuses == ("satisfied",)
    assert decision.outcome is KaniOutcome.VERIFIED


def test_an_unreachable_ownerless_cover_leaves_the_run_undetermined() -> None:
    """Count an undecided cover from the block even without an owner."""
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.cover.1", "SATISFIED", "reachable"),
        PropertyResult("cover.2", "UNREACHABLE", "other"),
        failed_count=0,
        total_count=0,
        covered_satisfied=1,
        covered_total=2,
        covered_unreachable=1,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, covers=("reachable",)),))
    decision = analyse_kani_log(log, manifest)
    assert decision.witness_statuses == ("satisfied",)
    assert decision.outcome is KaniOutcome.UNDETERMINED


def test_an_unresolved_foreign_check_leaves_the_run_undetermined() -> None:
    """Count a check the solver could not decide, whoever owns it.

    The verdict rests on what the called routine proves, so an assertion there
    that the solver left unresolved proves no more than one of the harness's
    own.  Reporting ``verified`` would claim a result the log does not support.
    """
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other.assertion.1", "UNDETERMINED", "foreign"),
        failed_count=0,
        total_count=2,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.UNDETERMINED


def test_an_unreachable_foreign_check_is_not_this_blocks_evidence() -> None:
    """Leave another routine's unreachable check out of the block's evidence.

    Its unreachability says that routine was not reached on this path, which
    is that routine's fact.  Only an unreachable check the harness owns, or one
    CBMC printed without an owner, shows that nothing was proved here.
    """
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other.cover.1", "UNREACHABLE", "foreign"),
        failed_count=0,
        total_count=2,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.VERIFIED


def test_an_unreachable_foreign_assertion_is_not_this_blocks_evidence() -> None:
    """Leave another routine's unreachable assertion out of the evidence too.

    The rule is not about covers: any check owned by a called routine is that
    routine's fact, so its unreachability cannot show that this harness proved
    nothing.  An implementation that only special-cased covers would fail here.
    """
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other.assertion.1", "UNREACHABLE", "foreign"),
        failed_count=0,
        total_count=2,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.VERIFIED


def test_an_expected_panic_excuses_a_foreign_failure() -> None:
    """Exempt a called routine's failure from a claim declared to panic.

    A panic aborts the routine it happens in and unwinds through the harness,
    so a check that fails beneath the claim is the expected panic seen one
    frame down.  Denying the claim for it would report the outcome the manifest
    asked for as a counterexample.
    """
    name = "h::p"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other.assertion.1", "FAILURE", "foreign"),
        failed_count=1,
        total_count=2,
        panics_as_expected=True,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, should_panic=True),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.VERIFIED


@pytest.mark.parametrize(
    "failing",
    ["h::p.overflow.1", "other.overflow.1"],
    ids=["owned", "foreign"],
)
def test_an_expected_panic_excuses_only_the_checks_it_notes(failing: str) -> None:
    """Deny the claim for a failure the note cannot explain.

    Kani prints the note only when every failed property is an assertion, so a
    failed check of another kind in the same block is an independent failure
    the note does not cover, whether the harness or a called routine owns it.
    Excusing it would report verified beside a safety failure.
    """
    name = "h::p"
    block = shapes.block(
        name,
        PropertyResult(failing, "FAILURE", "arithmetic overflow on signed +"),
        failed_count=1,
        total_count=1,
        panics_as_expected=True,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, should_panic=True),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.COUNTEREXAMPLE


def test_a_foreign_failure_without_an_expected_panic_is_a_counterexample() -> None:
    """Refuse to excuse a called failure no manifest entry asked for."""
    name = "h::p"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        PropertyResult("other.assertion.1", "FAILURE", "foreign"),
        failed_count=1,
        total_count=2,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM),))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.COUNTEREXAMPLE


def test_a_repeated_witness_description_must_all_be_satisfied() -> None:
    """Require every cover sharing a description to be satisfied."""
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.cover.1", "UNREACHABLE", "the witness"),
        PropertyResult("cover.2", "SATISFIED", "the witness"),
        failed_count=0,
        total_count=0,
        covered_satisfied=1,
        covered_total=2,
        covered_unreachable=1,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM, covers=("the witness",)),))
    decision = analyse_kani_log(log, manifest)
    assert decision.witness_statuses == ("unsatisfied",)


def test_a_passing_unsupported_construct_check_is_not_unsupported() -> None:
    """Require the check to have failed before it can deny a claim."""
    name = "h::c"
    block = shapes.block(
        name,
        PropertyResult(
            f"{name}.unsupported_construct.1",
            "SUCCESS",
            "the construct was modelled",
        ),
        failed_count=0,
        total_count=1,
    )
    log = shapes.verified_log(block)
    manifest = Manifest((Harness(name, CLAIM),))
    assert analyse_kani_log(log, manifest).outcome == KaniOutcome.VERIFIED
