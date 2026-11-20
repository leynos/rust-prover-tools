"""Prove the Kani analysis implements the precedence over every fact combination.

The analysis is what callers ultimately trust, so sampling a few logs is not
evidence enough.  The suite enumerates the full cartesian product of
decision-relevant per-harness classes for one, two and three harnesses - every
claim outcome, every witness state, every control disposition and an undeclared
harness - and holds the product to three properties: outcomes never improve
under degradation, a counterexample dominates everything below it, and
``verified`` holds exactly when every claim stands.

A data table pins the cases whose *reason* matters, so a precedence change is a
table edit rather than a test rewrite.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.kani_log import (
    CLAIM,
    CONTROL,
    FOCUSED,
    FULL_PROFILE,
    Harness,
    KaniAnalysis,
    KaniLog,
    KaniLogError,
    KaniOutcome,
    Manifest,
    analyse_kani_log,
    from_kani_log_error,
)
from tests import kani_log_corpus as corpus
from tests import kani_log_fixtures as facts
from tests import kani_log_properties as props

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_every_table_row_decides_as_it_declares() -> None:
    """Compare every row's outcome with what the analysis reports."""
    for row in corpus.table()["rows"]:
        decision = facts.decide_row(row)
        assert decision.outcome == row["outcome"], (
            f"{row['id']} must decide {row['outcome']}: {row['why']}"
        )


def test_every_table_row_explains_itself() -> None:
    """Require each row to carry the reason it exists."""
    for row in corpus.table()["rows"]:
        assert row.get("why"), f"{row['id']} must state why its outcome is right"


def test_the_table_covers_every_outcome() -> None:
    """Require the table to reach every outcome the analysis can return."""
    covered = {row["outcome"] for row in corpus.table()["rows"]}
    assert covered == {outcome.value for outcome in KaniOutcome}


def test_counterexamples_dominate() -> None:
    """Require a failed claim assertion to decide counterexample regardless."""
    checked = 0
    wrong = []
    for combination in props.declared_combinations():
        if not any(
            fact.role == CLAIM
            and fact.status == facts.FAILURE
            and not (fact.should_panic and fact.note)
            for fact in combination
        ):
            continue
        checked += 1
        if facts.decision_for(combination).outcome != KaniOutcome.COUNTEREXAMPLE:
            wrong.append(props.names_of(combination))
    assert wrong == [], "a failed claim assertion must decide counterexample"
    assert checked > 100, "the product must reach counterexample many times"


def test_degrading_never_improves_the_outcome() -> None:
    """Require a worse harness never to turn the run verified."""
    improved = []
    for combination in props.declared_combinations():
        if facts.decision_for(combination).outcome == KaniOutcome.VERIFIED:
            continue
        for index in range(len(combination)):
            worse = props.degrade(combination, index)
            if facts.decision_for(worse).outcome == KaniOutcome.VERIFIED:
                improved.append(props.names_of(combination))
    assert improved == [], "degrading a harness must never turn the run verified"


def test_verified_exactly_when_every_claim_stands() -> None:
    """Require verified if and only if every claim stands."""
    disagreeing = [
        props.names_of(combination)
        for combination in props.declared_combinations()
        if (facts.decision_for(combination).outcome == KaniOutcome.VERIFIED)
        != props.claims_stand(combination)
    ]
    assert disagreeing == [], "verified must hold exactly when every claim stands"


def test_an_unsatisfied_witness_is_always_reported() -> None:
    """Require every declared cover to appear with its own status."""
    disagreeing = [
        props.names_of(combination)
        for combination in props.declared_combinations()
        if facts.decision_for(combination).witness_statuses
        != props.declared_witnesses(combination)
    ]
    assert disagreeing == [], "every declared cover must be reported"


def test_every_outcome_is_reachable() -> None:
    """Require the product to reach every outcome."""
    reached = {
        facts.decision_for(combination).outcome
        for combination in props.every_combination()
    }
    assert reached == set(KaniOutcome)


def test_an_undeclared_harness_is_always_a_tool_error() -> None:
    """Require any combination naming a role-less harness to be refused."""
    checked = 0
    wrong = []
    for combination in props.every_combination():
        if props.well_formed(combination):
            continue
        checked += 1
        decision = facts.decision_for(combination)
        if (
            decision.outcome != KaniOutcome.TOOL_ERROR
            or "does not declare" not in decision.reason
        ):
            wrong.append(props.names_of(combination))
    assert wrong == [], "a harness with no role must be a tool error naming it"
    assert checked > 100, "the product must reach an undeclared harness many times"


def test_witness_statuses_follow_the_declared_covers() -> None:
    """Require one witness per declared cover, in the manifest's order."""
    disagreeing = [
        props.names_of(combination)
        for combination in props.declared_combinations()
        if len(facts.decision_for(combination).witness_statuses)
        != len(facts.manifest_of(combination).declared_covers())
        or not set(facts.decision_for(combination).witness_statuses)
        <= set(props.WITNESSES)
    ]
    assert disagreeing == [], "there must be one admitted status per cover"


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


def test_each_control_reports_its_disposition() -> None:
    """Require one disposition per control, drawn from the admitted set."""
    disagreeing = [
        props.names_of(combination)
        for combination in props.declared_combinations()
        if not props.controls_well_formed(combination)
    ]
    assert disagreeing == [], "every control must report one admitted disposition"


def test_a_control_never_becomes_a_claim() -> None:
    """Require a control's own failure to leave the claims' verdict alone."""
    combination = (facts.HarnessFact("h::k", CONTROL, facts.AS_INTENDED),)
    decision = facts.decision_for(combination)
    assert (decision.outcome, decision.control_statuses) == (
        KaniOutcome.VERIFIED,
        ("rejected-as-intended",),
    )


@pytest.mark.parametrize("name", ["selected_harness", "verified"])
def test_a_single_harness_run_is_never_full_profile(name: str) -> None:
    """Require a selected single harness to run in focused mode."""
    assert corpus.corpus_decision_for(name).run_mode == FOCUSED


def test_the_corpus_contains_a_full_profile_run() -> None:
    """Require at least one committed log to reach the full profile."""
    decisions = {name: corpus.corpus_decision_for(name) for name in corpus.log_names()}
    full_profile = {
        name
        for name, decision in decisions.items()
        if decision.run_mode == FULL_PROFILE
    }
    assert full_profile == {"default_unwind", "insufficient_unwind"}
    reaching_bounded = {
        name for name in full_profile if decisions[name].outcome == KaniOutcome.VERIFIED
    }
    assert reaching_bounded == {"default_unwind"}


def test_full_profile_requires_every_manifest_harness() -> None:
    """Require the whole manifest, with unwinding enabled, to be full-profile."""
    combination = tuple(
        facts.HarnessFact(
            fact.name,
            fact.role,
            fact.status,
            fact.cover,
            fact.expected,
            fact.should_panic,
            fact.note,
            bounded=True,
        )
        for fact in (
            facts.HarnessFact("h::a", CLAIM, facts.SUCCESS),
            facts.HarnessFact("h::k", CONTROL, facts.AS_INTENDED),
        )
    )
    assert facts.decision_for(combination).run_mode == FULL_PROFILE


def test_full_profile_requires_enabled_unwinding_checks() -> None:
    """Require an unbounded run to stay focused even when the manifest is covered."""
    combination = (
        facts.HarnessFact("h::a", CLAIM, facts.SUCCESS),
        facts.HarnessFact("h::k", CONTROL, facts.AS_INTENDED),
    )
    assert facts.decision_for(combination).run_mode == FOCUSED


def test_a_narrowed_manifest_is_what_makes_a_run_full_profile() -> None:
    """Require the same log to be full-profile for its run and focused for the crate."""
    assert corpus.corpus_decision_for("insufficient_unwind").run_mode == FULL_PROFILE
    whole = analyse_kani_log(
        corpus.parse_log("insufficient_unwind"),
        corpus.corpus_manifest(),
    )
    assert whole.run_mode == FOCUSED


def test_a_structural_refusal_decides_tool_error() -> None:
    """Require the killed run to decide tool-error with its structural reason."""
    decision = corpus.corpus_decision_for("truncated")
    assert decision.outcome == KaniOutcome.TOOL_ERROR
    assert "truncated" in decision.reason


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
    } <= outcomes
    assert {
        KaniOutcome.UNSUPPORTED,
        KaniOutcome.UNDETERMINED,
    } & outcomes
    assert any("unsatisfied" in decision.witness_statuses for decision in decisions)


def test_a_successful_run_may_still_have_an_unsatisfied_witness() -> None:
    """Require the vacuous run's successful verdict not to decide verified."""
    decision = corpus.corpus_decision_for("vacuous_assumption")
    assert decision.outcome == KaniOutcome.UNDETERMINED
    assert decision.witness_statuses == ("unsatisfied",)


def broken_always_verified(
    _log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis that ignores the log entirely."""
    return KaniAnalysis(KaniOutcome.VERIFIED)


def broken_success_line_only(
    log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis that trusts Kani's printed verdict and nothing else."""
    blocks = log.blocks
    outcome = (
        KaniOutcome.VERIFIED
        if all(block.succeeded for block in blocks)
        else KaniOutcome.COUNTEREXAMPLE
    )
    return KaniAnalysis(outcome)


def broken_manifest_ignored(
    log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis taken against a manifest invented from the log itself."""
    harnesses = log.harnesses
    if not harnesses:
        return broken_always_verified(log, _manifest)
    invented = Manifest(
        tuple(Harness(name, CLAIM) for name in harnesses),
    )
    return analyse_kani_log(log, invented)


def _apply(
    decide_function: cabc.Callable[[KaniLog, Manifest], KaniAnalysis],
    name: str,
) -> KaniAnalysis:
    """Run one broken analysis over one corpus log, refusing nothing it cannot parse."""
    try:
        log = corpus.parse_log(name)
    except KaniLogError as error:
        return from_kani_log_error(error)
    return decide_function(log, corpus.manifest_for(name))


def test_the_real_decision_gets_nothing_wrong() -> None:
    """Establish the reference the broken analyses are measured against."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(corpus.corpus_decision_for(name)) != corpus.recorded(name)
    ]
    assert wrong == []


def test_always_verified_is_wrong() -> None:
    """Reject an analysis that ignores the log and reports success."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_always_verified, name))
        != corpus.recorded(name)
    ]
    assert wrong


def test_the_success_line_alone_is_wrong() -> None:
    """Reject an analysis that trusts the printed verdict and ignores covers."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_success_line_only, name))
        != corpus.recorded(name)
    ]
    assert "vacuous_assumption" in wrong


def test_ignoring_the_manifest_is_wrong() -> None:
    """Reject an analysis that never reads roles, witnesses or expected failures."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_manifest_ignored, name))
        != corpus.recorded(name)
    ]
    assert "seeded_wrapping_mutant" in wrong
