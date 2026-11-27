"""Hold the evidence sinks and their record formatting to their contract."""

from __future__ import annotations

import io
import json
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.supervision import (
    MIN_LINE_BYTES,
    Diagnostics,
    HumanSink,
    JsonlSink,
    Measurement,
    MemorySink,
    OutputLine,
    ResourceSample,
    SupervisionConfig,
    bounded_text,
    decode_jsonl,
    fit_record,
    utc_text,
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
from rust_prover_tools.unittests.supervision_evidence_support import (
    _record,
    _report,
    _result,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


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


class _FlushFails(io.StringIO):
    """A stream whose flush always fails, as a full disk would."""

    def flush(self) -> None:
        """Refuse the flush."""
        msg = "flush failed"
        raise OSError(msg)


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


def test_a_human_budget_below_the_marker_is_refused() -> None:
    """A human line cannot be bounded below its truncation marker."""
    with pytest.raises(ValueError, match="at least"):
        HumanSink(io.StringIO(), max_line_bytes=1)


def test_a_non_utf8_jsonl_stream_is_refused() -> None:
    """Offsets are byte offsets only for the documented UTF-8 contract."""
    handle = io.TextIOWrapper(io.BytesIO(), encoding="utf-16")

    with pytest.raises(ValueError, match="UTF-8"):
        JsonlSink(handle)


def test_the_offset_query_does_not_repair_the_destination(
    tmp_path: Path,
) -> None:
    """The query names the next offset; only the repair writes a boundary."""
    from rust_prover_tools.supervision import (
        jsonl_next_offset,
        repair_jsonl_boundary,
    )

    path = tmp_path / "evidence.jsonl"
    path.write_text("x", encoding="utf-8")

    assert jsonl_next_offset(path) == 2
    assert path.read_bytes() == b"x", "the query wrote to the destination"
    assert repair_jsonl_boundary(path) == 2
    assert path.read_bytes() == b"x\n"
    assert repair_jsonl_boundary(path) == 2
    assert path.read_bytes() == b"x\n"


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


def test_a_budget_below_the_truncation_marker_is_refused() -> None:
    """A caller cannot request a line bound that cannot carry its marker."""
    with pytest.raises(ValueError, match="at least"):
        bounded_text("x" * 100, 8)


@given(st.text(max_size=200), st.integers(min_value=64, max_value=512))
@settings(max_examples=50, deadline=None)
def test_bounded_text_stays_within_its_encoded_limit(
    text: str,
    limit: int,
) -> None:
    """Arbitrary text is bounded by encoded bytes, and short text is kept."""
    bounded = bounded_text(text, limit)

    assert len(bounded.encode("utf-8")) <= limit
    if len(text.encode("utf-8")) <= limit:
        assert bounded == text


@given(st.text(max_size=300), st.integers(min_value=MIN_LINE_BYTES, max_value=1024))
@settings(max_examples=50, deadline=None)
def test_fit_record_output_is_valid_json_within_its_limit(
    text: str,
    limit: int,
) -> None:
    """A record that does not fit is still serialized as valid bounded JSON."""
    payload: dict[str, object] = {
        "schema": 1,
        "record": "output",
        "run_id": "run-1",
        "text": text,
        "nested": {"description": text},
    }

    line = fit_record(payload, limit)

    assert len(line.encode("utf-8")) <= limit
    json.loads(line)


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
