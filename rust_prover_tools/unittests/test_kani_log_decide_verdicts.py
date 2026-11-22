"""Prove the precedence holds on the edge cases and on the committed corpus.

The product tests in ``test_kani_log_decide`` enumerate what the analysis can
say about constructed blocks.  These cases pin the readings those blocks rest
on - what a failed verdict means when no check explains it, and what a timeout
means when one does - and then compare every committed log with the decision it
is recorded to reach, so a reading that changes silently changes the record.
"""

from __future__ import annotations

from rust_prover_tools.kani_log import (
    CLAIM,
    Harness,
    HarnessBlock,
    KaniLog,
    KaniOutcome,
    Manifest,
    ManualSummary,
    PropertyResult,
    analyse_kani_log,
)
from tests import kani_log_corpus as corpus
from tests import kani_log_decide_fixtures as shapes
from tests import kani_log_properties as props


def test_a_failed_verdict_outweighs_a_missing_claim() -> None:
    """Prefer the disproof a failed block shows to the skip the log also shows.

    A block Kani declared failed carries a failure somewhere.  If none of the
    attributed checks explains it, reporting the run as merely skipped would
    bury that disproof beneath a harness that never ran.
    """
    name = "h::a"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        failed_count=0,
        total_count=1,
        verdict="FAILED",
    )
    log = KaniLog("0.67.0", "regular", (block,), ManualSummary(0, 1, 1, (name,)), ())
    manifest = Manifest((Harness(name, CLAIM), Harness("h::b", CLAIM)))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.COUNTEREXAMPLE, (
        "Prefer the disproof a failed block shows to the skip the log also shows."
    )


def test_a_failed_verdict_outranks_another_claims_timeout() -> None:
    """Prefer an unexplained failure to a timeout in a different claim.

    The precedence ranks a counterexample above resource exhaustion.  A block
    Kani declared failed with every attributed check passing carries a
    disproof the analysis cannot see; a sibling claim's timeout is the weaker
    fact and must not decide the run.
    """
    name = "h::a"
    block = shapes.block(
        name,
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "own"),
        failed_count=0,
        total_count=1,
        verdict="FAILED",
    )
    timed_out = HarnessBlock(name="h::b", verdict="FAILED", timed_out=True)
    log = KaniLog(
        "0.67.0",
        "regular",
        (block, timed_out),
        ManualSummary(0, 2, 2, (name, "h::b")),
        (),
    )
    manifest = Manifest((Harness(name, CLAIM), Harness("h::b", CLAIM)))
    assert analyse_kani_log(log, manifest).outcome is KaniOutcome.COUNTEREXAMPLE, (
        "Prefer an unexplained failure to a timeout in a different claim."
    )


def test_a_timeout_without_an_unexplained_failure_is_exhaustion() -> None:
    """Keep a timeout's own classification when no verdict is unexplained."""
    timed_out = HarnessBlock(name="h::a", verdict="FAILED", timed_out=True)
    log = KaniLog(
        "0.67.0",
        "regular",
        (timed_out,),
        ManualSummary(0, 1, 1, ("h::a",)),
        (),
    )
    manifest = Manifest((Harness("h::a", CLAIM),))
    decision = analyse_kani_log(log, manifest)
    assert decision.outcome is KaniOutcome.RESOURCE_EXHAUSTED, (
        "Keep a timeout's own classification when no verdict is unexplained."
    )


def test_a_structural_refusal_decides_tool_error() -> None:
    """Require the killed run to decide tool-error with its structural reason."""
    decision = corpus.corpus_decision_for("truncated")
    assert decision.outcome == KaniOutcome.TOOL_ERROR, (
        "Require the killed run to decide tool-error with its structural reason."
    )
    assert "truncated" in decision.reason, (
        "Require the killed run to decide tool-error with its structural reason."
    )


def test_every_committed_log_decides_as_recorded() -> None:
    """Compare each analysis's outcome, witnesses, controls and run mode."""
    for name in corpus.log_names():
        assert props.summarise(corpus.corpus_decision_for(name)) == corpus.recorded(
            name,
        ), f"{name} must decide exactly what it is recorded to decide"


def test_the_corpus_reaches_the_required_classes() -> None:
    """Require the corpus to contain each outcome class registration depends on."""
    decisions = [corpus.corpus_decision_for(name) for name in corpus.log_names()]
    outcomes = {decision.outcome for decision in decisions}
    assert {
        KaniOutcome.VERIFIED,
        KaniOutcome.COUNTEREXAMPLE,
        KaniOutcome.RESOURCE_EXHAUSTED,
        KaniOutcome.SKIPPED,
        KaniOutcome.TOOL_ERROR,
    } <= outcomes, (
        "Require the corpus to contain each outcome class registration depends on."
    )
    assert {
        KaniOutcome.UNSUPPORTED,
        KaniOutcome.UNDETERMINED,
    } & outcomes, (
        "Require the corpus to contain each outcome class registration depends on."
    )
    assert any("unsatisfied" in decision.witness_statuses for decision in decisions), (
        "Require the corpus to contain each outcome class registration depends on."
    )


def test_a_successful_run_may_still_have_an_unsatisfied_witness() -> None:
    """Require the vacuous run's successful verdict not to decide verified."""
    decision = corpus.corpus_decision_for("vacuous_assumption")
    assert decision.outcome == KaniOutcome.UNDETERMINED, (
        "Require the vacuous run's successful verdict not to decide verified."
    )
    assert decision.witness_statuses == ("unsatisfied",), (
        "Require the vacuous run's successful verdict not to decide verified."
    )
