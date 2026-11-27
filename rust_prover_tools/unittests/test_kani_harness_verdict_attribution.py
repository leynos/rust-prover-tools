"""Hold live-fact and synthetic-observation verdict attribution."""

from __future__ import annotations

from rust_prover_tools.kani_log import parse_kani_log_lines
from rust_prover_tools.supervision import (
    ExecutionStatus,
    OutputLine,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.kani import (
    KaniAdapter,
    KaniObservation,
    KaniObserver,
)
from rust_prover_tools.supervision.kani_outcome import observed_verdict
from rust_prover_tools.unittests.kani_harness_classifier_support import (
    _evidence,
    _finish,
    _live_facts,
    _log,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock


def test_live_fact_overflow_leaves_a_stopped_verdict_unknown() -> None:
    """A dropped live record makes the retained verdict unattributable.

    The observer's budget is filled directly, because description content can
    no longer spend it; the guarded property is that a truncated fact set
    yields no attributed verdict on a stop.
    """
    from rust_prover_tools.supervision.kani_observer import MAX_LIVE_RECORDS

    observer = KaniObserver()
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Checking harness harnesses::verified...",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )
    observer.observe(
        OutputLine(
            stream="stdout",
            text="VERIFICATION:- SUCCESSFUL",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )
    for index in range(MAX_LIVE_RECORDS):
        observer._scanner._record(
            observer.selection_records, f"harnesses::filler{index}"
        )
    assert observer.facts_truncated
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_stopped_attempt_with_a_dropped_duplicate_is_unknown() -> None:
    """The overflow guard must run before a stop returns.

    One matching selection and verdict are retained, the budget is filled by
    other names, and a second matching selection is dropped. A timeout then
    stops the attempt before any parse, so only the pre-stop guard can keep
    the retained `PASSED` from being reported.
    """
    from rust_prover_tools.supervision.kani_observer import MAX_LIVE_RECORDS

    observer = KaniObserver()
    clock = VirtualClock()
    lines = [
        "Kani Rust Verifier 0.67.0 (cargo plugin)",
        "Checking harness harnesses::verified...",
        "VERIFICATION:- SUCCESSFUL",
        *(
            f"Checking harness harnesses::filler{index}..."
            for index in range(MAX_LIVE_RECORDS - 1)
        ),
        "Checking harness harnesses::verified...",
    ]
    for line in lines:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    assert observer.facts_truncated
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_duplicate_live_selection_leaves_the_verdict_unattributable() -> None:
    """Two live selection records for one pretty name cannot take a verdict.

    The observer never records verdicts itself, but `observed_verdict` owns the
    attribution rule for supplied facts; without the single-selection guard an
    ambiguous attempt would be credited with `PASSED`.
    """
    facts = _live_facts(
        selections=("harnesses::verified", "harnesses::verified"),
        verdicts=(("harnesses::verified", "SUCCESSFUL"),),
    )

    verdict = observed_verdict(_evidence("harnesses::verified"), facts)

    assert verdict is ProofVerdict.UNKNOWN


def test_duplicate_live_verdicts_leave_the_verdict_unattributable() -> None:
    """Two live verdicts for one selection are ambiguous."""
    facts = _live_facts(
        selections=("harnesses::verified",),
        verdicts=(
            ("harnesses::verified", "SUCCESSFUL"),
            ("harnesses::verified", "FAILED"),
        ),
    )

    verdict = observed_verdict(_evidence("harnesses::verified"), facts)

    assert verdict is ProofVerdict.UNKNOWN


def test_one_live_selection_and_verdict_are_attributed() -> None:
    """Exactly one selection and one verdict name the attempt's outcome."""
    facts = _live_facts(
        selections=("harnesses::verified",),
        verdicts=(("harnesses::verified", "SUCCESSFUL"),),
    )

    verdict = observed_verdict(_evidence("harnesses::verified"), facts)

    assert verdict is ProofVerdict.PASSED


def test_a_synthetic_observation_classifies_without_an_observer() -> None:
    """The classifier takes the observation value, not a live observer.

    Building the value directly pins the seam: everything classification reads
    is in the observation, so a caller can classify a corpus log or a refusal
    without ever constructing an observer.
    """
    log = parse_kani_log_lines(_log("verified", "harnesses::verified"))
    facts = _live_facts(selections=("harnesses::verified",))
    observation = KaniObservation(facts=facts, log=log)

    classification = KaniAdapter().classify(
        _evidence("harnesses::verified"), observation
    )

    assert classification.status is ExecutionStatus.PASSED
    assert classification.proof_verdict is ProofVerdict.PASSED
    assert classification.observed == ("alpha::harnesses::verified",)


def test_a_synthetic_parse_refusal_classifies_as_malformed_output() -> None:
    """A refusal carried by the observation is the classifier's only reason."""
    observation = KaniObservation(
        facts=_live_facts(selections=("harnesses::verified",)),
        log=None,
        failure_reason="the log does not begin with a Kani banner",
    )

    classification = KaniAdapter().classify(
        _evidence("harnesses::verified"), observation
    )

    assert classification.status is ExecutionStatus.ERROR
    assert classification.detail.startswith("malformed verifier output:")
    assert "does not begin with a Kani banner" in classification.detail
