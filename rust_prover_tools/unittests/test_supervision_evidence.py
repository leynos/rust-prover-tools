"""Hold the evidence sinks and the versioned schemas to their contract."""

from __future__ import annotations

import dataclasses as dc
import io
import json
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.supervision import (
    MIN_LINE_BYTES,
    REPORT_NOTES,
    Diagnostics,
    DiagnosticSinks,
    EvidenceFormatError,
    ExecutionStatus,
    HarnessResult,
    HarnessSpec,
    HumanSink,
    InventoryDrift,
    JsonlSink,
    Measurement,
    MemorySink,
    OutputLine,
    ProofVerdict,
    ResourceSample,
    RunOutcome,
    RunReport,
    StopReason,
    SupervisionConfig,
    bounded_text,
    decode_jsonl,
    decode_measurement,
    fit_record,
    render_table,
    report_document,
    utc_text,
    validate_report_document,
    write_report,
)
from rust_prover_tools.supervision.events import (
    RunStart,
    harness_finish_record,
    harness_start_record,
    heartbeat_record,
    output_record,
    run_finish_record,
    run_start_record,
    sample_record,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock, spec


def _record(**fields: object) -> dict[str, object]:
    """Return a minimal run-start record with the given overrides."""
    base: dict[str, object] = {
        "record": "run-start",
        "run_id": "run-1",
        "timestamp": "2026-11-21T12:00:00.000Z",
        "packages": ["example"],
        "harness_count": 1,
        "timeout_seconds": 1.0,
        "sample_interval_seconds": 1.0,
        "heartbeat_interval_seconds": 1.0,
        "watchdog_mib": 1024,
    }
    base.update(fields)
    return base


def test_utc_text_is_explicit_and_stable() -> None:
    """A timestamp names UTC and carries milliseconds."""
    clock = VirtualClock()

    assert utc_text(clock.now()) == "2026-11-21T12:00:00.000Z"


def test_bounded_text_marks_a_truncated_line() -> None:
    """A cut line is visibly cut rather than quietly short."""
    text = bounded_text("x" * 100, 20)

    assert len(text.encode("utf-8")) <= 20
    assert text.endswith("…[truncated]")


def test_bounded_text_leaves_a_short_line_alone() -> None:
    """A line inside its budget is unchanged."""
    assert bounded_text("short", 20) == "short"


class _FlushRecorder(io.StringIO):
    """A text stream that counts flushes."""

    def __init__(self) -> None:
        """Create a recorder that has flushed nothing yet."""
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        """Count the flush and defer to the base implementation."""
        self.flushes += 1
        super().flush()


def test_the_jsonl_sink_writes_a_versioned_flushed_record() -> None:
    """Every record carries the schema and is flushed before returning."""
    handle = _FlushRecorder()
    sink = JsonlSink(handle)

    sink.write(_record())

    assert handle.getvalue().endswith("\n")
    assert handle.flushes >= 1, "the record was not flushed"
    document = json.loads(handle.getvalue())
    assert document["schema"] == 1
    assert document["record"] == "run-start"
    assert sink.offset == len(handle.getvalue().encode("utf-8"))


def test_an_oversized_record_remains_valid_json() -> None:
    """Truncation shortens fields rather than cutting the JSON line."""
    handle = io.StringIO()
    sink = JsonlSink(handle, max_line_bytes=256)

    sink.write(_record(run_id="x" * 10_000))

    records = decode_jsonl(handle.getvalue())
    assert len(records) == 1
    assert records[0]["record"] in {"run-start", "truncated"}
    if records[0]["record"] == "run-start":
        assert "…[truncated]" in str(records[0]["run_id"])
    else:
        assert records[0]["marker"] == "…[truncated]"


def test_the_fallback_record_fits_the_minimum_budget() -> None:
    """Even the minimal truncation record obeys the configured budget."""
    handle = io.StringIO()
    sink = JsonlSink(handle, max_line_bytes=MIN_LINE_BYTES)

    sink.write(_record(run_id="x" * 10_000))

    assert len(handle.getvalue().encode("utf-8")) <= MIN_LINE_BYTES
    assert len(decode_jsonl(handle.getvalue())) == 1


def test_a_diagnostic_reference_needs_an_offset() -> None:
    """A nested diagnostic reference obeys the same strict key set."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["diagnostic"] = {"sink": "jsonl"}

    with pytest.raises(EvidenceFormatError, match="missing"):
        validate_report_document(document)


def test_an_appended_sink_keeps_absolute_offsets() -> None:
    """A diagnostic reference names the file offset, not the run offset."""
    handle = io.StringIO()
    sink = JsonlSink(handle, start_offset=4096)

    sink.write(_record())

    assert sink.offset == 4096 + len(handle.getvalue().encode("utf-8"))


def test_u2028_inside_a_record_does_not_split_it() -> None:
    """JSONL records are separated by LF, not every Unicode line boundary."""
    sink = MemorySink()
    output: dict[str, object] = {
        "record": "output",
        "run_id": "run-1",
        "package": "example",
        "harness": "h::x",
        "attempt": 1,
        "timestamp": "2026-11-21T12:00:00.000Z",
        "elapsed_seconds": 0.0,
        "stream": "stdout",
        "text": "before\u2028after",
    }
    sink.write(output)

    assert len(decode_jsonl(sink.text)) == 1


def test_a_finish_record_with_an_empty_measurement_is_refused() -> None:
    """The finish-record measurements are validated like every other record."""
    sink = MemorySink()
    record = harness_finish_record(run_id="run-1", result=_result())
    record["peak_group_rss"] = {}
    sink.write(record)

    with pytest.raises(EvidenceFormatError, match="neither"):
        decode_jsonl(sink.text)


def test_the_human_sink_renders_a_readable_line() -> None:
    """The human log names the run, timeout, sampling, and watchdog."""
    handle = io.StringIO()
    sink = HumanSink(handle)

    sink.write(_record())

    line = handle.getvalue()
    assert "run run-1 start" in line
    assert "timeout=1.0s" in line
    assert "watchdog=1024MiB" in line


def test_a_closed_human_reader_does_not_fail_the_final_flush() -> None:
    """Undeliverable human output must not prevent report finalization."""

    class _Closed(io.StringIO):
        """A text stream whose reader has gone away."""

        def flush(self) -> None:
            """Refuse the flush, as a closed pipe does."""
            raise BrokenPipeError

    HumanSink(_Closed()).flush()


def test_the_human_sink_survives_a_closed_reader() -> None:
    """A reader that goes away must not fail the proof."""

    class _Closed(io.StringIO):
        """A text stream whose reader has gone away."""

        def write(self, payload: str, /) -> int:
            """Refuse the write, as a closed pipe does."""
            raise BrokenPipeError

    HumanSink(_Closed()).write(_record())


def test_diagnostics_returns_the_jsonl_offset() -> None:
    """The offset names where a harness's evidence begins."""
    jsonl = MemorySink()
    human = io.StringIO()
    diagnostics = Diagnostics(jsonl=jsonl, human=HumanSink(human))

    first = diagnostics.emit(_record())
    second = diagnostics.emit(_record(run_id="run-2"))

    assert first is not None
    assert second is not None
    assert first.offset == 0
    assert second.offset > first.offset
    assert len(jsonl.records) == 2


def _result() -> HarnessResult:
    """Return one complete passing result."""
    return HarnessResult(
        spec=HarnessSpec("example", "harnesses::verified"),
        status=ExecutionStatus.PASSED,
        stop_reason=StopReason.EXITED,
        exit_code=0,
        signal_number=None,
        proof_verdict=ProofVerdict.PASSED,
        started_at=VirtualClock().now(),
        finished_at=VirtualClock().now(),
        duration_seconds=1.25,
        peak_group_rss=Measurement.available(128 * 1024 * 1024),
        min_host_available=Measurement.available(8 * 1024 * 1024 * 1024),
        host_total=Measurement.available(16 * 1024 * 1024 * 1024),
        cgroup=None,
        last_output="VERIFICATION:- SUCCESSFUL",
        last_progress="Verification Time: 1.2s",
        diagnostic=None,
        detail="",
    )


def _report() -> RunReport:
    """Return one complete passing report."""
    clock = VirtualClock()
    return RunReport(
        schema_version=1,
        run_id="run-1",
        started_at=clock.now(),
        finished_at=clock.now(),
        expected=(HarnessSpec("example", "harnesses::verified"),),
        results=(_result(),),
        accounting=InventoryDrift(),
        outcome=RunOutcome.PASSED,
    )


def test_the_report_document_carries_the_schema_and_notes() -> None:
    """The final report is versioned and states its measurement limits."""
    document = report_document(_report())

    assert document["schema"] == 1
    assert document["passed"] is True
    assert document["notes"] == list(REPORT_NOTES)
    validate_report_document(document)


def test_an_unknown_report_key_is_refused() -> None:
    """A schema that is not enforced drifts."""
    document = report_document(_report())
    document["surprise"] = 1

    with pytest.raises(EvidenceFormatError, match="unknown"):
        validate_report_document(document)


def test_a_missing_report_key_is_refused() -> None:
    """A report that omits a documented field is refused."""
    document = report_document(_report())
    del document["outcome"]

    with pytest.raises(EvidenceFormatError, match="missing"):
        validate_report_document(document)


def test_a_measurement_that_is_neither_form_is_refused() -> None:
    """An empty measurement object is not evidence."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["peak_group_rss"] = {}

    with pytest.raises(EvidenceFormatError, match="neither"):
        validate_report_document(document)


def test_a_report_is_written_atomically(tmp_path: Path) -> None:
    """The destination holds a complete JSON document after the call."""
    destination = tmp_path / "report.json"

    write_report(destination, _report())

    document = json.loads(destination.read_text(encoding="utf-8"))
    validate_report_document(document)
    assert not list(tmp_path.glob("*.tmp"))


def test_every_record_kind_can_be_decoded() -> None:
    """The strict reader accepts exactly the shipped record shapes."""
    clock = VirtualClock()
    identity = spec()
    sample = ResourceSample(
        timestamp_utc=clock.now(),
        elapsed_seconds=1.0,
        package=identity.package,
        harness=identity.harness,
        attempt=identity.attempt,
        group_rss=Measurement.available(1),
        host_available=Measurement.available(2),
        host_total=Measurement.available(3),
    )
    sink = MemorySink()
    diagnostics = Diagnostics(jsonl=sink)
    config = SupervisionConfig()
    for record in (
        run_start_record(
            RunStart(
                run_id="run-1",
                timestamp=clock.now(),
                packages=("example",),
                harness_count=1,
                config=config,
            )
        ),
        harness_start_record(run_id="run-1", spec=identity, timestamp=clock.now()),
        output_record(
            run_id="run-1",
            spec=identity,
            line=OutputLine(
                stream="stdout",
                text="hello",
                timestamp_utc=clock.now(),
                elapsed_seconds=0.5,
            ),
        ),
        sample_record(run_id="run-1", sample=sample),
        heartbeat_record(
            run_id="run-1", sample=sample, idle_seconds=0.0, last_progress=None
        ),
        harness_finish_record(run_id="run-1", result=_result()),
        run_finish_record(run_id="run-1", timestamp=clock.now(), report=_report()),
    ):
        diagnostics.emit(record)

    records = decode_jsonl(sink.text)

    assert [record["record"] for record in records] == [
        "run-start",
        "harness-start",
        "output",
        "sample",
        "heartbeat",
        "harness-finish",
        "run-finish",
    ]


def test_an_unknown_record_kind_is_refused() -> None:
    """A reader refuses a record it does not understand."""
    sink = MemorySink()
    sink.write({"record": "surprise", "run_id": "run-1"})

    with pytest.raises(EvidenceFormatError, match="unknown kind"):
        decode_jsonl(sink.text)


def test_the_table_names_every_result_and_the_limits() -> None:
    """The human summary carries the identity, status, and measurement notes."""
    lines = render_table(_report())

    assert "PACKAGE" in lines[0]
    assert "HARNESS" in lines[0]
    assert any("harnesses::verified" in line for line in lines)
    assert any(line.startswith("note:") for line in lines)
    assert any("outcome=passed" in line for line in lines)


def test_open_diagnostics_starts_at_a_line_boundary(tmp_path: Path) -> None:
    """A file without a final newline gains one before the next record."""
    from rust_prover_tools.kani_diagnostics import open_diagnostics

    path = tmp_path / "evidence.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))

    with open_diagnostics(config, None) as diagnostics:
        reference = diagnostics.emit(_record())

    assert reference is not None
    assert reference.offset == 101
    assert path.read_text(encoding="utf-8").splitlines()[0] == "x" * 100


def test_a_run_finish_record_with_malformed_accounting_is_refused() -> None:
    """The nested accounting object obeys its documented key set."""
    sink = MemorySink()
    record = run_finish_record(
        run_id="run-1", timestamp=VirtualClock().now(), report=_report()
    )
    record["accounting"] = {}
    sink.write(record)

    with pytest.raises(EvidenceFormatError, match="missing"):
        decode_jsonl(sink.text)


def test_a_negative_measurement_is_refused() -> None:
    """A byte measurement cannot be negative."""
    with pytest.raises(EvidenceFormatError, match="negative"):
        decode_measurement({"value": -1})


def test_the_fallback_keeps_the_original_kind_when_it_fits() -> None:
    """A bounded fallback keeps the lost record's kind when the budget allows."""
    handle = io.StringIO()
    sink = JsonlSink(handle, max_line_bytes=100)

    sink.write(_record(run_id="x" * 10_000))

    record = decode_jsonl(handle.getvalue())[0]
    assert len(handle.getvalue().encode("utf-8")) <= 100
    if record["record"] == "truncated":
        assert record.get("original_kind") == "run-start"


def test_truncation_preserves_the_record_kind() -> None:
    """A shortened record keeps the field the reader dispatches on."""
    payload: dict[str, object] = {
        "schema": 1,
        "record": "run-start",
        "run_id": "r" * 16,
        "packages": [f"package-{index:04d}" for index in range(100)],
    }

    line = fit_record(payload, 2200)

    decoded = json.loads(line)
    assert decoded["record"] == "run-start"
    assert len(line.encode("utf-8")) <= 2200


@pytest.mark.parametrize("schema", [True, 1.0, "1", None])
def test_a_jsonl_schema_must_be_an_exact_integer(schema: object) -> None:
    """A bool or float comparing equal to 1 is not the versioned schema."""
    sink = MemorySink()
    sink.write(_record(schema=schema))

    with pytest.raises(EvidenceFormatError):
        decode_jsonl(sink.text)


def test_a_report_schema_must_be_an_exact_integer() -> None:
    """The report reader applies the same strict version check."""
    document = report_document(_report())
    document["schema"] = True

    with pytest.raises(EvidenceFormatError):
        validate_report_document(document)


def test_a_human_budget_below_the_marker_is_refused() -> None:
    """A human line cannot be bounded below its truncation marker."""
    with pytest.raises(ValueError, match="at least"):
        HumanSink(io.StringIO(), max_line_bytes=1)


def test_a_non_utf8_jsonl_stream_is_refused() -> None:
    """Offsets are byte offsets only for the documented UTF-8 contract."""
    handle = io.TextIOWrapper(io.BytesIO(), encoding="utf-16")

    with pytest.raises(ValueError, match="UTF-8"):
        JsonlSink(handle)


def test_jsonl_append_offset_adds_a_missing_newline(tmp_path: Path) -> None:
    """A destination without a final newline gains one before appending."""
    from rust_prover_tools.supervision import jsonl_append_offset

    path = tmp_path / "evidence.jsonl"
    path.write_text("x", encoding="utf-8")

    assert jsonl_append_offset(path) == 2
    assert path.read_bytes() == b"x\n"
    assert jsonl_append_offset(path) == 2


def test_truncation_preserves_the_invocation_identity() -> None:
    """A shortened record still joins to its harness by the documented keys."""
    payload: dict[str, object] = {
        "schema": 1,
        "record": "output",
        "run_id": "r" * 16,
        "package": "alpha",
        "harness": "h" * 80,
        "attempt": 1,
        "text": "x" * 4000,
    }

    decoded = json.loads(fit_record(payload, 256))

    assert decoded["record"] == "output"
    assert decoded["run_id"] == "r" * 16
    assert decoded["package"] == "alpha"
    assert decoded["harness"] == "h" * 80


def test_accounting_fields_must_be_arrays() -> None:
    """A scalar in place of a drift list is not a valid report."""
    document = report_document(_report())
    document["accounting"] = {
        "missing": None,
        "duplicate": 0,
        "unexpected": "none",
    }

    with pytest.raises(EvidenceFormatError, match="array"):
        validate_report_document(document)


class _FlushFails(io.StringIO):
    """A stream whose flush always fails, as a full disk would."""

    def flush(self) -> None:
        """Refuse the flush."""
        msg = "flush failed"
        raise OSError(msg)


def test_a_failed_jsonl_sink_stops_issuing_offsets() -> None:
    """A stale offset is worse than no reference after a failed flush."""
    human = MemorySink()
    jsonl = JsonlSink(_FlushFails())
    diagnostics = Diagnostics(human=human, jsonl=jsonl)

    first = diagnostics.emit(_record())

    assert first is None
    assert jsonl.failed
    assert diagnostics.emit(_record()) is None
    assert human.records


def test_a_failed_report_publication_keeps_the_previous_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atomic rename cannot destroy a good report when it fails."""
    path = tmp_path / "report.json"
    path.write_text("previous report", encoding="utf-8")

    def fail(self: Path, target: Path) -> None:
        """Refuse the publication rename."""
        del self, target
        msg = "rename failed"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", fail)

    with pytest.raises(OSError, match="rename failed"):
        write_report(path, _report())

    assert path.read_text(encoding="utf-8") == "previous report"
    assert not list(tmp_path.glob(".report.json.*.tmp"))


def test_a_budget_below_the_truncation_marker_is_refused() -> None:
    """A caller cannot request a line bound that cannot carry its marker."""
    with pytest.raises(ValueError, match="at least"):
        bounded_text("x" * 100, 8)


def test_a_multiline_detail_stays_on_one_table_row() -> None:
    """A detail cannot split its row into unrelated log lines."""
    report = _report()
    result = report.results[0]
    report = dc.replace(
        report,
        results=(dc.replace(result, detail="first\nsecond"),),
    )

    lines = render_table(report)

    assert all("\n" not in line for line in lines)
    assert any("first second" in line for line in lines)
