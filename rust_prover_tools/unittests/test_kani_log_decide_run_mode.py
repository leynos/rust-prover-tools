"""Prove the run mode and selection rules read the declaration, not the log.

``full`` means the run covered the manifest it was asked about with unwinding
checks enabled.  That is a relation between the log and the declaration, not a
property of the log alone: the same log is full for a narrowed ``--harness``
selection and focused for the whole crate.  A block the selection left out is
skipped rather than refused, because the declaration - not the selection -
decides which harnesses exist.
"""

from __future__ import annotations

from rust_prover_tools.kani_log import (
    CLAIM,
    CONTROL,
    FOCUSED,
    FULL_PROFILE,
    Harness,
    KaniOutcome,
    Manifest,
    analyse_kani_log,
)
from tests import kani_log_corpus as corpus
from tests import kani_log_decide_fixtures as shapes
from tests import kani_log_fixtures as facts


def test_a_single_harness_run_is_full_profile_for_its_own_manifest() -> None:
    """Require a run narrowed to one harness to cover that manifest completely."""
    assert corpus.corpus_decision_for("selected_harness").run_mode == FULL_PROFILE


def test_the_corpus_contains_a_full_profile_run() -> None:
    """Require only the runs that omit a declared harness to stay focused."""
    decisions = {name: corpus.corpus_decision_for(name) for name in corpus.log_names()}
    focused = {
        name for name, decision in decisions.items() if decision.run_mode == FOCUSED
    }
    assert focused == {"fail_fast", "list", "truncated"}
    verified = {
        name
        for name, decision in decisions.items()
        if decision.run_mode == FULL_PROFILE
        and decision.outcome == KaniOutcome.VERIFIED
    }
    assert {"default_unwind", "verified"} <= verified


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
            unwinding_checks=True,
        )
        for fact in (
            facts.HarnessFact("h::a", CLAIM, facts.SUCCESS, unwinding_checks=True),
            facts.HarnessFact(
                "h::k", CONTROL, facts.AS_INTENDED, unwinding_checks=True
            ),
        )
    )
    assert facts.decision_for(combination).run_mode == FULL_PROFILE


def test_full_profile_requires_enabled_unwinding_checks() -> None:
    """Require a run that disabled unwinding checks to stay focused."""
    combination = (
        facts.HarnessFact("h::a", CLAIM, facts.SUCCESS, unwinding_checks=False),
        facts.HarnessFact("h::k", CONTROL, facts.AS_INTENDED, unwinding_checks=False),
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


def test_a_block_for_an_unselected_harness_is_skipped_not_refused() -> None:
    """Skip a block the run did not select instead of calling it undeclared.

    ``--harness`` narrows the manifest after the declaration, so a log from a
    wider run reports harnesses the analysis was not asked about. The
    declaration, not the selection, is what decides whether a block is
    undeclared.
    """
    log = shapes.log_of("h::a", "h::b")
    whole = Manifest((Harness("h::a", CLAIM), Harness("h::b", CLAIM)))
    decision = analyse_kani_log(
        log,
        whole.select(("h::a",)),
        declared=whole.names(),
    )
    assert decision.outcome is KaniOutcome.VERIFIED
    assert decision.run_mode == FULL_PROFILE


def test_a_block_the_declaration_does_not_name_is_still_refused() -> None:
    """Refuse a block for a harness the manifest never declared at all."""
    log = shapes.log_of("h::a", "h::stray")
    manifest = Manifest((Harness("h::a", CLAIM),))
    decision = analyse_kani_log(log, manifest)
    assert decision.outcome is KaniOutcome.TOOL_ERROR
    assert "does not declare" in decision.reason
