"""Prove the Kani analysis implements the precedence over every fact combination.

The analysis is what callers ultimately trust, so sampling a few logs is not
evidence enough.  The suite enumerates the full cartesian product of
decision-relevant per-harness classes for one, two and three harnesses - every
claim outcome, every witness state, every control disposition and an undeclared
harness - and holds the product to four properties: outcomes never improve under
degradation, a counterexample dominates everything below it, ``verified`` holds
exactly when every claim stands, and every witness and control the manifest
declares is reported.

A data table pins the cases whose *reason* matters, so a precedence change is a
table edit rather than a test rewrite.  The per-block reading these properties
depend on is tested by concern in the sibling ``ownership``, ``controls``,
``run_mode``, ``verdicts`` and ``mutants`` modules.
"""

from __future__ import annotations

from rust_prover_tools.kani_log import CLAIM, KaniOutcome
from tests import kani_log_corpus as corpus
from tests import kani_log_fixtures as facts
from tests import kani_log_properties as props
from tests import kani_log_rows


def test_every_table_row_decides_as_it_declares() -> None:
    """Compare every row's outcome with what the analysis reports."""
    for row in corpus.table()["rows"]:
        decision = kani_log_rows.decide_row(row)
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


def test_each_control_reports_its_disposition() -> None:
    """Require one disposition per control, drawn from the admitted set."""
    disagreeing = [
        props.names_of(combination)
        for combination in props.declared_combinations()
        if not props.controls_well_formed(combination)
    ]
    assert disagreeing == [], "every control must report one admitted disposition"
