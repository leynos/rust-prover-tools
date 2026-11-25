"""Hold the Kani classifier to its documented precedence and exact identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from rust_prover_tools.kani_cli_grammar import UnsupportedKaniGrammarError
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

_CORPUS = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs" / "0.67.0"
)


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


def test_a_stopped_harness_does_not_claim_an_unparsed_verdict() -> None:
    """A timeout reports its own status and leaves the verdict unknown.

    Kani escapes nothing inside a description, so a live verdict can be prose.
    The verdict is therefore attributed only from a parsed log, and a stopped
    attempt reports `UNKNOWN` rather than a claim the supervisor cannot check.
    """
    identity = "harnesses::verified"
    evidence = _evidence(identity, stop=StopReason.TIMEOUT, exit_code=None)
    adapter = KaniAdapter(version="0.67.0")

    classification = adapter.classify(evidence, _observer(identity))

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_nonzero_exit_with_no_output_is_not_a_selection_diagnosis() -> None:
    """A child that never printed says nothing about which harness matched."""
    adapter = KaniAdapter(version="0.67.0")
    identity = "harnesses::x"
    evidence = _evidence(identity, exit_code=2)

    classification = adapter.classify(evidence, KaniObserver())

    assert classification.status is ExecutionStatus.EXIT_NONZERO
    assert "no output" in classification.detail


def test_a_banner_only_failure_is_not_a_no_match() -> None:
    """A banner proves Kani started, not that selection completed."""
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

    assert classification.status is ExecutionStatus.EXIT_NONZERO


def test_kanis_own_no_match_diagnostic_is_a_selection_diagnosis() -> None:
    """Kani stating that nothing matched is selection evidence."""
    evidence = _evidence("harnesses::absent", exit_code=1)
    observer = KaniObserver()
    for text in (
        "Kani Rust Verifier 0.67.0 (cargo plugin)",
        "*no harnesses matched the harness filter: `harnesses::absent`",
    ):
        observer.observe(
            OutputLine(
                stream="stdout",
                text=text,
                timestamp_utc=VirtualClock().now(),
                elapsed_seconds=0.0,
            )
        )

    classification = KaniAdapter(version="0.67.0").classify(evidence, observer)

    assert classification.status is ExecutionStatus.NO_MATCH
    assert "no harnesses matched" in classification.detail


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
    assert observer.finish(0) is None
    assert "retained budget" in (observer.failure_reason or "")


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

    log = observer.finish(0)

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
    classification = KaniAdapter(version="0.67.0").classify(evidence, observer)
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
    assert observer.finish(0) is None


def test_a_duplicated_pretty_name_is_a_multiple_match() -> None:
    """Two targets reporting one pretty name credit neither.

    Measured with Kani 0.67.0: a library and a test copy of one name printed
    two selection records and one success plus one failure. The pair must not
    be reduced to a single pass, and the request is not silently satisfied by
    either record.
    """
    log = _log("verified", "collision_proof")
    first = log.index("Checking harness collision_proof...")
    last = next(
        index for index, line in enumerate(log) if line.startswith("Verification Time:")
    )
    block = log[first : last + 1]
    observer = KaniObserver()
    clock = VirtualClock()
    for line in [*log[:first], *block, *block, *log[last + 1 :]]:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    adapter = KaniAdapter()

    classification = adapter.classify(_evidence("collision_proof"), observer)

    assert classification.status is ExecutionStatus.MULTIPLE_MATCH
    assert classification.proof_verdict is ProofVerdict.UNKNOWN
    assert "2" in classification.detail


def test_a_selection_shaped_description_line_is_not_a_selection() -> None:
    """Description content cannot add a selection record.

    Kani prints descriptions without escaping newlines, so a proof message can
    contain a line that looks exactly like a selection record. The parsed log
    knows the frame; the live scanner does not. Classification must trust the
    parse, or a passing single-harness run fails as ``multiple-match``.
    """
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Checking harness fake...",
        'last"',
    ]
    observer = KaniObserver()
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
    assert "fake" not in observer.facts.selection_records
    adapter = KaniAdapter()

    classification = adapter.classify(_evidence("harnesses::verified"), observer)

    assert classification.status is ExecutionStatus.PASSED
    assert classification.observed == ("alpha::harnesses::verified",)


def test_a_signalled_child_with_a_passing_log_is_exit_nonzero() -> None:
    """A complete passing log does not hide a terminating signal.

    A child the supervisor signalled has no exit status, so the termination
    must be read from the signal rather than reported as missing evidence.
    """
    result = _classify(
        "harnesses::verified",
        exit_code=None,
        signal=15,
    )

    assert result.status is ExecutionStatus.EXIT_NONZERO
    assert result.proof_verdict is ProofVerdict.PASSED
    assert "signal 15" in result.detail


def test_a_silent_zero_exit_is_an_error_not_exit_nonzero() -> None:
    """A zero exit with no verifier output is missing evidence."""
    evidence = _evidence("harnesses::x", exit_code=0)

    classification = KaniAdapter().classify(evidence, KaniObserver())

    assert classification.status is ExecutionStatus.ERROR
    assert "no verifier output" in classification.detail


def test_a_stopped_attempt_releases_its_spool() -> None:
    """Classification releases the spool on a path that never parses it.

    A timed-out attempt returns before the parse, so an unclosed spool would
    keep its temporary directory alive for as long as the caller retains the
    observer.
    """
    observer = _observer("harnesses::verified")
    spool = observer._path
    assert spool is not None
    assert spool.exists()
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert not spool.exists()
    observer.close()
    observer.close()


def _log_with_selection_noise(count: int) -> list[str]:
    """Return a passing log whose description holds `count` fake selections."""
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    noise = [f"Checking harness fake{index}..." for index in range(count)]
    log[description : description + 1] = [
        '\t - Description: "first',
        *noise,
        'last"',
    ]
    return log


def _observed_log(log: list[str]) -> KaniObserver:
    """Return an observer fed one prepared log."""
    observer = KaniObserver()
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
    return observer


def test_description_noise_cannot_exhaust_the_live_facts() -> None:
    """Description content is excluded, so it cannot spend the record budget.

    A description quoting more selection-shaped lines than the budget holds
    leaves the live facts intact, and the parsed report still passes.
    """
    from rust_prover_tools.supervision.kani_observer import MAX_LIVE_RECORDS

    observer = _observed_log(_log_with_selection_noise(MAX_LIVE_RECORDS + 10))
    assert not observer.facts_truncated
    adapter = KaniAdapter()

    classification = adapter.classify(_evidence("harnesses::verified"), observer)

    assert classification.status is ExecutionStatus.PASSED
    assert classification.observed == ("alpha::harnesses::verified",)


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
        observer._record(observer.selection_records, f"harnesses::filler{index}")
    assert observer.facts_truncated
    evidence = _evidence(
        "harnesses::verified",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

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

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_spool_failure_is_not_replaced_by_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush or parse error survives a failing cleanup.

    A full temporary filesystem can fail both the flush and the close. The
    caller must see the primary failure, not the cleanup's.
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

    monkeypatch.setattr(observer._spool, "flush", fail_flush)
    stack = observer._stack
    assert stack is not None
    monkeypatch.setattr(stack, "close", fail_close)

    with pytest.raises(OSError, match="flushed"):
        observer.finish(0)


def test_a_description_verdict_does_not_become_a_live_verdict() -> None:
    """A quoted verdict line is description content, not a result.

    The attempt is interrupted before its genuine verdict, so a live verdict
    recorded from the description would be the only one and would publish
    `PASSED` for a proof the verifier never reported.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    verdict = next(
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    )
    observer = _observed_log(log[:verdict])
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_description_banner_does_not_replace_the_opening_release() -> None:
    """Only the log's opening banner names the release."""
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Kani Rust Verifier 0.68.0 (cargo plugin)",
        'last"',
    ]
    observer = _observed_log(log)
    assert observer.banner_version == "0.67.0"

    classification = KaniAdapter().classify(_evidence("harnesses::verified"), observer)

    assert classification.status is ExecutionStatus.PASSED


def test_a_parsed_mismatch_does_not_keep_a_live_verdict() -> None:
    """The parse is authoritative for the verdict as well as the identities.

    A description can contain a selection-shaped line and a verdict for the
    requested name while the verifier actually ran another harness. The
    parsed log names only `other`, so the mismatch must report no attributed
    verdict.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Checking harness harnesses::requested...",
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    observer = _observed_log(log)

    classification = KaniAdapter().classify(
        _evidence("harnesses::requested", exit_code=1), observer
    )

    assert classification.status is ExecutionStatus.NO_MATCH
    assert classification.proof_verdict is ProofVerdict.UNKNOWN
    assert classification.observed == ("alpha::harnesses::other",)


def test_a_description_ending_in_a_quote_still_hides_its_verdict() -> None:
    """An embedded quote is not evidence that the description ended.

    Kani escapes nothing, so a description can contain a line ending in a
    quotation mark followed by a verdict-shaped line. The attempt stops before
    its genuine verdict, and the live verdict must stay unknown.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first embedded"',
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    assert len(verdicts) == 2
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_an_ordinary_continuation_does_not_commit_a_description_verdict() -> None:
    """Description content followed by ordinary text stays description content.

    Kani escapes nothing, so a quoted verdict line followed by plain text and
    then `last"` is still one description. The frame closes at the real
    `Location:` field, so nothing from the description is credited.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first embedded"',
        "VERIFICATION:- SUCCESSFUL",
        "ordinary continuation",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_stopped_attempt_does_not_claim_a_verdict_from_prose() -> None:
    """No live verdict is attributed, so description prose cannot fabricate one.

    A description quoting `RESULTS:` and a verdict is ambiguous to any
    line-level scan. A stopped attempt therefore reports `UNKNOWN` unless its
    log parsed.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "RESULTS:",
        "VERIFICATION:- SUCCESSFUL",
        "ordinary continuation",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, observer)

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_complete_log_still_supplies_its_verdict() -> None:
    """The parser, not the live scan, attributes a genuine verdict."""
    observer = _observer("harnesses::verified")

    classification = KaniAdapter().classify(_evidence("harnesses::verified"), observer)

    assert classification.status is ExecutionStatus.PASSED
    assert classification.proof_verdict is ProofVerdict.PASSED
