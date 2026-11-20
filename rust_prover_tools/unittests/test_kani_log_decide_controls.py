"""Prove a control's disposition follows the failure the manifest seeded.

A control harness is run to fail, and the manifest names the check expected to
fail.  The run is only evidence that the seeded fault was caught: a failure of
anything else in the block means the control fired for the wrong reason, so it
is rejected rather than counted as intended.  A control also never contributes
to the claims' verdict, whatever its own outcome.
"""

from __future__ import annotations

from rust_prover_tools.kani_log import (
    CONTROL,
    REJECTED_OTHERWISE,
    FailedCheck,
    Harness,
    KaniLog,
    KaniOutcome,
    Manifest,
    ManualSummary,
    PropertyResult,
    analyse_kani_log,
)
from tests import kani_log_decide_fixtures as shapes
from tests import kani_log_fixtures as facts


def test_a_control_never_becomes_a_claim() -> None:
    """Require a control's own failure to leave the claims' verdict alone."""
    combination = (facts.HarnessFact("h::k", CONTROL, facts.AS_INTENDED),)
    decision = facts.decision_for(combination)
    assert (decision.outcome, decision.control_statuses) == (
        KaniOutcome.VERIFIED,
        ("rejected-as-intended",),
    )


def _control_log(*checks: PropertyResult) -> KaniLog:
    """Build a one-control log whose block reports exactly ``checks``."""
    name = "h::k"
    block = shapes.block(
        name,
        *checks,
        failures=tuple(FailedCheck(item.description, None, None) for item in checks),
        failed_count=sum(1 for item in checks if item.status == "FAILURE"),
        total_count=len(checks),
        verdict="FAILED",
    )
    return KaniLog(
        "0.67.0",
        "regular",
        (block,),
        ManualSummary(0, 1, 1, (name,)),
        (),
    )


def test_a_control_with_an_extra_failure_is_rejected_otherwise() -> None:
    """Require every failed check to match the declared expectation."""
    log = _control_log(
        PropertyResult("h::k.assertion.1", "FAILURE", "the seeded fault fires"),
        PropertyResult("h::k.assertion.2", "FAILURE", "an unrelated check fails"),
    )
    manifest = Manifest((Harness("h::k", CONTROL, expected_failure="assertion.1"),))
    assert analyse_kani_log(log, manifest).control_statuses == (REJECTED_OTHERWISE,)


def test_a_control_that_fails_a_called_property_is_rejected_otherwise() -> None:
    """Count a failure owned by a called routine as an unexpected failure."""
    log = _control_log(
        PropertyResult("h::k.assertion.1", "FAILURE", "the seeded fault fires"),
        PropertyResult(
            "core::fmt::Arguments::<'_>::from_str.assertion.1",
            "FAILURE",
            "attempt to shift left with overflow",
        ),
    )
    manifest = Manifest((Harness("h::k", CONTROL, expected_failure="assertion.1"),))
    assert analyse_kani_log(log, manifest).control_statuses == (REJECTED_OTHERWISE,)
