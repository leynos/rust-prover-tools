"""Hold the Kani observer spool and its retention budget to their lifecycle."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.supervision import (
    ExecutionStatus,
    HarnessSpec,
    OutputLine,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.kani import (
    KaniAdapter,
    KaniObserver,
)
from rust_prover_tools.unittests.kani_harness_classifier_support import (
    _evidence,
    _finish,
    _log,
    _no_spool,
    _observer,
    _RecordingSpool,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.kani_observer import TemporaryKaniSpool


def _unclosed_observer() -> tuple[KaniObserver, _RecordingSpool]:
    """Return an observer over a recording spool, plus that spool."""
    spool = _RecordingSpool()
    return KaniObserver(spool_factory=lambda: spool), spool


def test_a_log_over_the_retained_budget_is_truncated_evidence() -> None:
    """A log beyond the budget is reported, not retained without bound."""
    observer = KaniObserver(max_log_bytes=64)
    for text in (
        "Kani Rust Verifier 0.67.0 (cargo plugin)",
        "Checking harness harnesses::verified...",
        "VERIFICATION:- SUCCESSFUL",
        "padding " * 40,
    ):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=text,
                timestamp_utc=VirtualClock().now(),
                elapsed_seconds=0.0,
            )
        )

    assert observer.log_truncated
    observation = _finish(observer)
    assert observation.log is None
    assert "retained budget" in (observation.failure_reason or "")


def test_a_log_within_the_budget_is_parsed() -> None:
    """A log inside the budget reaches the shared parser unchanged."""
    identity = "harnesses::verified"
    observer = KaniObserver()
    for text in _log("verified", identity):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=text,
                timestamp_utc=VirtualClock().now(),
                elapsed_seconds=0.0,
            )
        )

    log = _finish(observer).log

    assert log is not None
    assert log.harnesses == (identity,)


def test_the_live_fact_budget_fails_closed() -> None:
    """Repeated selection records cannot grow the live facts without bound."""
    from rust_prover_tools.supervision.kani_observer import MAX_LIVE_RECORDS

    observer = KaniObserver()
    for index in range(MAX_LIVE_RECORDS + 5):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=f"Checking harness harnesses::h{index}...",
                timestamp_utc=VirtualClock().now(),
                elapsed_seconds=0.0,
            )
        )

    assert observer.facts_truncated
    assert len(observer.selection_records) == MAX_LIVE_RECORDS
    evidence = _evidence("harnesses::h0", exit_code=1)
    classification = KaniAdapter(version="0.67.0").classify(evidence, _finish(observer))
    assert classification.status is ExecutionStatus.ERROR
    assert "selection records" in classification.detail


def test_live_facts_continue_after_the_spool_stops() -> None:
    """Overflow stops the spool, not the live facts, which have their own cap.

    The log parser can no longer see a harness named after overflow, but the
    live scanner still records it so the run can account for what the verifier
    started.
    """
    observer = KaniObserver(max_log_bytes=64)
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Kani Rust Verifier 0.67.0 (cargo plugin)",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )
    observer.observe(
        OutputLine(
            stream="stdout",
            text="padding " * 20,
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )
    assert observer.log_truncated
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Checking harness harnesses::late...",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )

    assert observer.selection_records == ["harnesses::late"]
    assert _finish(observer).log is None


def test_a_stopped_attempt_releases_its_spool() -> None:
    """The attempt boundary releases the spool that classification never owns.

    A timed-out attempt classifies without parsing, so the close at the attempt
    boundary is the only cleanup. Classification must not release the spool on
    its own: the observation it reads is immutable, and the spool belongs to
    the observer that produced it.
    """
    observer = _observer("harnesses::verified")
    spool = typ.cast("TemporaryKaniSpool", observer._spool)
    assert spool._path.exists()
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    observation = observer.finish()
    classification = KaniAdapter().classify(evidence, observation)

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert spool._path.exists()
    observer.close()
    observer.close()
    assert not spool._path.exists()
    observer.close()
    observer.close()


def test_a_spool_close_failure_does_not_replace_a_stop() -> None:
    """A cleanup failure while stopping is suppressed, not substituted.

    The observation is finished before the spool is released, so a stop must
    still report the reason and detail it already holds even when the close at
    the attempt boundary fails.
    """

    class _FailingClose(KaniObserver):
        """An observer whose spool cleanup fails."""

        def close(self) -> None:
            """Fail as a closed spool file would."""
            msg = "the spool could not be closed"
            raise OSError(msg)

    observer = _FailingClose()
    clock = VirtualClock()
    for line in (
        "Kani Rust Verifier 0.67.0 (cargo plugin)",
        "Checking harness harnesses::verified...",
    ):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out after 30s",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.detail == "timed out after 30s"


def test_a_spool_failure_is_recorded_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flush failure becomes a named observation, not an escaping error.

    `finish` runs while an attempt may be unwinding a cancellation or a
    monitor fault, so it must not raise: a full temporary filesystem names
    the failure in the observation instead. The cleanup failure from `close`
    still reaches the attempt boundary that owns the resource.
    """
    observer = KaniObserver()
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Kani Rust Verifier 0.67.0 (cargo plugin)",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )

    def fail_flush() -> None:
        """Fail the flush as a full filesystem would."""
        msg = "the spool could not be flushed"
        raise OSError(msg)

    def fail_close() -> None:
        """Fail the cleanup that follows."""
        msg = "the spool could not be closed"
        raise OSError(msg)

    spool = typ.cast("TemporaryKaniSpool", observer._spool)
    monkeypatch.setattr(spool._handle, "flush", fail_flush)
    monkeypatch.setattr(spool, "close", fail_close)

    observation = observer.finish()

    assert observation.log is None
    assert observation.spool_error == "OSError: the spool could not be flushed"
    with pytest.raises(OSError, match="closed"):
        observer.close()


def test_classifying_an_observation_does_not_close_the_spool() -> None:
    """Classification reads the immutable observation and touches nothing else.

    The spool outlives classification so the attempt boundary, not the
    classifier, decides when to release it. The observer must stay usable and
    its facts must be exactly what the observation reported.
    """
    observer, spool = _unclosed_observer()
    for text in _log("verified", "harnesses::verified"):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=text,
                timestamp_utc=VirtualClock().now(),
                elapsed_seconds=0.0,
            )
        )
    observation = observer.finish()
    facts_before = observation.facts

    classification = KaniAdapter().classify(
        _evidence("harnesses::verified"), observation
    )

    assert classification.status is ExecutionStatus.PASSED
    assert spool.closed is False
    assert observer.facts == facts_before
    observer.close()
    assert spool.closed is True
    observer.close()


def test_a_spool_that_cannot_open_is_an_explicit_error() -> None:
    """A spool the attempt cannot open is named, not silently ignored.

    The verifier's live facts survive the failure, but a classification that
    cannot retain the log is an error rather than a pass with missing
    evidence.
    """
    adapter = KaniAdapter(spool_factory=_no_spool)
    observer = adapter.observer(HarnessSpec("alpha", "harnesses::verified"))
    observer.observe(
        OutputLine(
            stream="stdout",
            text="Kani Rust Verifier 0.67.0 (cargo plugin)",
            timestamp_utc=VirtualClock().now(),
            elapsed_seconds=0.0,
        )
    )
    observation = observer.finish()
    assert observation.spool_error == "OSError: no spool"

    classification = adapter.classify(_evidence("harnesses::verified"), observation)

    assert classification.status is ExecutionStatus.ERROR
    assert "cannot retain the verifier log" in classification.detail
    assert "no spool" in classification.detail
    observer.close()


def test_a_truncated_stopped_attempt_reports_no_verdict() -> None:
    """A shortened log supplies no verdict, even though the stop still wins."""
    log = _log("verified", "harnesses::verified")
    observer = KaniObserver(max_log_bytes=64)
    clock = VirtualClock()
    for line in log:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )

    classification = KaniAdapter().classify(
        _evidence("harnesses::verified", stop=StopReason.TIMEOUT, exit_code=None),
        _finish(observer),
    )

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.UNKNOWN
