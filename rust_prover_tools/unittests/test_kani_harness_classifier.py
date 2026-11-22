"""Hold the Kani classifier to its documented precedence and exact identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from rust_prover_tools.kani_log import UnsupportedKaniGrammarError
from rust_prover_tools.supervision import (
    CgroupObservation,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    Measurement,
    OutputLine,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.attempt_state import AttemptEvidence
from rust_prover_tools.supervision.kani import KaniAdapter, KaniObserver
from rust_prover_tools.unittests.supervision_doubles import VirtualClock

_CORPUS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs"


def _log(name: str, pretty: str) -> list[str]:
    """Return one corpus log with its first harness name rewritten to `pretty`."""
    import re

    text = (_CORPUS / f"{name}.log").read_text(encoding="utf-8")
    names = re.findall(r"Checking harness (\S+)\.\.\.", text)
    if names:
        text = text.replace(names[0], pretty)
    return text.splitlines()


def _observer(pretty: str, name: str = "verified") -> KaniObserver:
    """Return an observer fed one corpus log for `pretty`."""
    observer = KaniObserver()
    clock = VirtualClock()
    for line in _log(name, pretty):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    return observer


# pylint: disable-next=too-many-arguments
def _evidence(
    pretty: str,
    *,
    stop: StopReason = StopReason.EXITED,
    exit_code: int | None = 0,
    signal: int | None = None,
    launch_error: str | None = None,
    cgroup: CgroupObservation | None = None,
    detail: str = "",
) -> AttemptEvidence:
    """Return one attempt's termination evidence."""
    absent = Measurement.unavailable("not measured")
    clock = VirtualClock()
    return AttemptEvidence(
        spec=HarnessSpec("alpha", pretty),
        stop_reason=stop,
        exit_code=exit_code,
        signal_number=signal,
        launch_error=launch_error,
        started_at=clock.now(),
        finished_at=clock.now(),
        duration_seconds=1.0,
        samples=(),
        peak_group_rss=absent,
        min_host_available=absent,
        host_total=absent,
        cgroup=cgroup or CgroupObservation(path=None, start=None, finish=None),
        last_output=None,
        stop_detail=detail,
    )


# pylint: disable-next=too-many-arguments
def _classify(
    requested: str,
    name: str = "verified",
    *,
    observed: str | None = None,
    stop: StopReason = StopReason.EXITED,
    exit_code: int | None = 0,
    signal: int | None = None,
    launch_error: str | None = None,
    cgroup: CgroupObservation | None = None,
    detail: str = "",
) -> HarnessClassification:
    """Classify one rewritten corpus log."""
    adapter = KaniAdapter()
    return adapter.classify(
        _evidence(
            requested,
            stop=stop,
            exit_code=exit_code,
            signal=signal,
            launch_error=launch_error,
            cgroup=cgroup,
            detail=detail,
        ),
        _observer(observed or requested, name),
    )


def test_a_successful_single_harness_is_passed() -> None:
    """Success requires one exact record, a verified verdict, and exit zero."""
    result = _classify("harnesses::a")

    assert result.status is ExecutionStatus.PASSED
    assert result.proof_verdict is ProofVerdict.PASSED
    assert result.last_progress is not None


def test_a_counterexample_is_failed_not_exit_nonzero() -> None:
    """A proof failure outranks the nonzero status that usually accompanies it."""
    result = _classify("harnesses::a", "counterexample", exit_code=1)

    assert result.status is ExecutionStatus.FAILED
    assert result.proof_verdict is ProofVerdict.FAILED


def test_an_expected_panic_is_still_a_pass() -> None:
    """A should_panic harness's failed check is the documented success."""
    result = _classify("harnesses::a", "expected_panic")

    assert result.status is ExecutionStatus.PASSED
    assert result.proof_verdict is ProofVerdict.PASSED


def test_a_selection_with_no_records_is_no_match() -> None:
    """Zero checked harnesses cannot be the requested proof."""
    result = _classify("harnesses::a", "list")

    assert result.status is ExecutionStatus.NO_MATCH


def test_a_selection_of_several_records_is_multiple_match() -> None:
    """A filter that selected two harnesses is not an exact run."""
    result = _classify("harnesses::verified", "default_unwind")

    assert result.status is ExecutionStatus.MULTIPLE_MATCH


def test_a_single_wrong_identity_is_no_match() -> None:
    """A run of a different harness is a selection mismatch, not a pass."""
    result = _classify("harnesses::requested", observed="harnesses::other")

    assert result.status is ExecutionStatus.NO_MATCH
    assert "observed" in result.detail or "selected" in result.detail


def test_a_nonzero_exit_with_a_verified_log_is_exit_nonzero() -> None:
    """Exit zero is required for success even when the verdict looks good."""
    result = _classify("harnesses::a", exit_code=2)

    assert result.status is ExecutionStatus.EXIT_NONZERO
    assert result.proof_verdict is ProofVerdict.PASSED


def test_a_signalled_child_is_exit_nonzero() -> None:
    """A signal with no verdict is a termination failure."""
    result = _classify("harnesses::a", exit_code=-15, signal=15)

    assert result.status is ExecutionStatus.EXIT_NONZERO


def test_a_timeout_outranks_every_log_fact() -> None:
    """A supervisor stop is the termination evidence, whatever the log shows."""
    result = _classify(
        "harnesses::a",
        stop=StopReason.TIMEOUT,
        detail="harness exceeded its 10-second budget",
    )

    assert result.status is ExecutionStatus.TIMED_OUT
    assert "10-second budget" in result.detail


def test_a_watchdog_stop_records_the_oom_delta_separately() -> None:
    """A policy stop is not presented as proof of a kernel OOM kill."""
    from rust_prover_tools.supervision import CgroupMemory

    memory_start = CgroupMemory(
        current=Measurement.available(1),
        peak=Measurement.available(2),
        oom_kill=Measurement.available(0),
    )
    memory_finish = CgroupMemory(
        current=Measurement.available(1),
        peak=Measurement.available(2),
        oom_kill=Measurement.available(1),
    )
    cgroup = CgroupObservation(
        path="/user.slice/job.scope",
        start=memory_start,
        finish=memory_finish,
    )

    result = _classify(
        "harnesses::a",
        stop=StopReason.MEMORY_WATCHDOG,
        detail="host available memory fell below the floor",
        cgroup=cgroup,
    )

    assert result.status is ExecutionStatus.MEMORY_EXHAUSTED
    assert "oom_kill_delta=1" in result.detail
    assert "OOM" not in result.detail


def test_an_interruption_is_reported_as_interrupted() -> None:
    """An interrupted attempt is neither a proof failure nor a launch error."""
    result = _classify(
        "harnesses::a",
        stop=StopReason.INTERRUPTED,
        detail="a signal interrupted the harness",
    )

    assert result.status is ExecutionStatus.INTERRUPTED


def test_a_launch_failure_is_an_error() -> None:
    """A child that never started cannot have produced a counterexample."""
    result = _classify(
        "harnesses::a",
        stop=StopReason.LAUNCH_FAILED,
        exit_code=None,
        launch_error="driver missing",
    )

    assert result.status is ExecutionStatus.ERROR
    assert "driver missing" in result.detail


def test_a_truncated_log_is_malformed_output() -> None:
    """A log cut before its verdict cannot support a classification."""
    result = _classify("harnesses::a", "truncated")

    assert result.status is ExecutionStatus.ERROR
    assert result.proof_verdict is ProofVerdict.UNKNOWN
    assert result.detail.startswith("malformed verifier output:")
    assert "truncated" in result.detail


def test_an_unregistered_grammar_is_refused_before_observation() -> None:
    """Only the qualified release may produce a classification."""
    with pytest.raises(UnsupportedKaniGrammarError):
        KaniAdapter(version="0.68.0").observer(HarnessSpec("alpha", "h"))


def test_the_adapter_exports_are_the_documented_types() -> None:
    """The classifier and observer are the public adapter surface."""
    assert KaniAdapter().version == "0.67.0"


def test_a_passing_log_without_an_exit_status_is_an_error() -> None:
    """Success requires an explicit zero status, not an absent one."""
    result = _classify("harnesses::a", exit_code=None)

    assert result.status is ExecutionStatus.ERROR
    assert "no exit status" in result.detail


def test_an_unregistered_banner_becomes_typed_evidence() -> None:
    """A release without a grammar is an attempt error, not a run crash."""
    observer = KaniObserver()
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Kani Rust Verifier 0.68.0 (cargo plugin)",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )

    classification = KaniAdapter().classify(_evidence("alpha::harness"), observer)

    assert classification.status is ExecutionStatus.ERROR
    assert "0.68.0" in classification.detail


def test_a_stopped_harness_keeps_the_verdict_it_already_reported() -> None:
    """A timeout outranks the status, but not an observed proof verdict."""
    identity = "harnesses::verified"
    evidence = _evidence(identity, stop=StopReason.TIMEOUT, exit_code=None)
    adapter = KaniAdapter(version="0.67.0")

    classification = adapter.classify(evidence, _observer(identity))

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.PASSED


def test_a_nonzero_exit_with_no_output_is_not_a_selection_diagnosis() -> None:
    """A child that never printed says nothing about which harness matched."""
    adapter = KaniAdapter(version="0.67.0")
    identity = "harnesses::x"
    evidence = _evidence(identity, exit_code=2)

    classification = adapter.classify(evidence, KaniObserver())

    assert classification.status is ExecutionStatus.EXIT_NONZERO
    assert "no output" in classification.detail


def test_a_verifier_that_never_ran_is_not_a_no_match() -> None:
    """An explicit no-match needs the verifier to have printed its banner."""
    evidence = _evidence("harnesses::absent", exit_code=1)
    observer = KaniObserver()
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Kani Rust Verifier 0.67.0 (cargo plugin)",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )

    classification = KaniAdapter(version="0.67.0").classify(evidence, observer)

    assert classification.status is ExecutionStatus.NO_MATCH
