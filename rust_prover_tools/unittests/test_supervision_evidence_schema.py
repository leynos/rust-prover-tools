"""Hold the versioned evidence and report schemas to their strict reader."""

from __future__ import annotations

import io
import json
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    EvidenceFormatError,
    JsonlSink,
    MemorySink,
    decode_jsonl,
    decode_measurement,
    fit_record,
    report_document,
    validate_report_document,
)
from rust_prover_tools.supervision.events import (
    harness_finish_record,
    run_finish_record,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock, spec
from rust_prover_tools.unittests.supervision_evidence_support import (
    _record,
    _report,
    _result,
    _valid_record,
)


def test_a_diagnostic_reference_needs_an_offset() -> None:
    """A nested diagnostic reference obeys the same strict key set."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["diagnostic"] = {"sink": "jsonl"}

    with pytest.raises(EvidenceFormatError, match="missing"):
        validate_report_document(document)


def test_a_finish_record_with_an_empty_measurement_is_refused() -> None:
    """The finish-record measurements are validated like every other record."""
    sink = MemorySink()
    record = harness_finish_record(run_id="run-1", result=_result())
    record["peak_group_rss"] = {}
    sink.write(record)

    with pytest.raises(EvidenceFormatError, match="neither"):
        decode_jsonl(sink.text)


def test_an_unknown_record_kind_is_refused() -> None:
    """A reader refuses a record it does not understand."""
    sink = MemorySink()
    sink.write({"record": "surprise", "run_id": "run-1"})

    with pytest.raises(EvidenceFormatError, match="unknown kind"):
        decode_jsonl(sink.text)


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


@pytest.mark.parametrize("status", ["passing", "PASSED", "", 7])
def test_an_unknown_result_status_is_refused(status: object) -> None:
    """A status outside the documented set is not a report this reader accepts."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["status"] = status

    with pytest.raises(EvidenceFormatError, match="status"):
        validate_report_document(document)


@pytest.mark.parametrize("duration", ["1.5", True, -1.0, float("nan"), None])
def test_a_non_numeric_duration_is_refused(duration: object) -> None:
    """A duration is a finite non-negative number of seconds."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["duration_seconds"] = duration

    with pytest.raises(EvidenceFormatError, match="duration_seconds"):
        validate_report_document(document)


def test_an_unknown_outcome_is_refused() -> None:
    """The aggregate outcome is one of the documented values."""
    document = report_document(_report())
    document["outcome"] = "nearly-passed"

    with pytest.raises(EvidenceFormatError, match="outcome"):
        validate_report_document(document)


def test_an_unknown_proof_verdict_is_refused() -> None:
    """The proof verdict is one of the documented values."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["proof_verdict"] = "probably"

    with pytest.raises(EvidenceFormatError, match="proof_verdict"):
        validate_report_document(document)


def test_an_unknown_stop_reason_is_refused() -> None:
    """A stop reason outside the documented set is refused."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["stop_reason"] = "killed-somehow"

    with pytest.raises(EvidenceFormatError, match="stop_reason"):
        validate_report_document(document)


@pytest.mark.parametrize("field", ["complete", "passed", "interrupted"])
def test_a_non_boolean_report_flag_is_refused(field: str) -> None:
    """A Boolean flag is not an integer or a string."""
    document = report_document(_report())
    document[field] = 1

    with pytest.raises(EvidenceFormatError, match=field):
        validate_report_document(document)


def test_a_boolean_attempt_is_refused() -> None:
    """`True` is an `int` in Python, but it is not an attempt ordinal."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["attempt"] = True

    with pytest.raises(EvidenceFormatError, match="attempt"):
        validate_report_document(document)


def test_a_string_exit_code_is_refused() -> None:
    """An exit code is an integer or absent, never a string."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["exit_code"] = "0"

    with pytest.raises(EvidenceFormatError, match="exit_code"):
        validate_report_document(document)


def test_the_shipped_report_still_validates() -> None:
    """The stricter reader accepts what the producer writes."""
    validate_report_document(report_document(_report()))


@pytest.mark.parametrize("field", ["missing", "duplicate", "unexpected"])
@pytest.mark.parametrize("entry", [7, "", None, ["p::x"]])
def test_a_non_string_accounting_entry_is_refused(field: str, entry: object) -> None:
    """Every drift entry is a package-qualified identity string."""
    document = report_document(_report())
    accounting = typ.cast("dict[str, object]", document["accounting"])
    accounting[field] = [entry]

    with pytest.raises(EvidenceFormatError, match=field):
        validate_report_document(document)


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("output", "stream", 17),
        ("output", "text", 17),
        ("output", "elapsed_seconds", -1.0),
        ("output", "attempt", 0),
        ("output", "run_id", 17),
        ("heartbeat", "idle_seconds", -1.0),
        ("heartbeat", "last_progress", 17),
        ("harness-finish", "status", "invented"),
        ("harness-finish", "stop_reason", "perhaps"),
        ("harness-finish", "proof_verdict", "probably"),
        ("harness-finish", "duration_seconds", -1.0),
        ("harness-finish", "exit_code", "zero"),
        ("harness-finish", "last_output", 17),
    ],
)
def test_a_correctly_keyed_record_with_a_bad_value_is_refused(
    kind: str,
    field: str,
    value: object,
) -> None:
    """The key set alone must not admit an invented or negative value."""
    record = _valid_record(kind)
    record[field] = value

    with pytest.raises(EvidenceFormatError, match=field.split("_")[0]):
        decode_jsonl(json.dumps(record))


def test_a_run_finish_record_with_an_invented_outcome_is_refused() -> None:
    """A run outcome must be one the report model documents."""
    record: dict[str, object] = {
        "schema": 1,
        "record": "run-finish",
        "run_id": "run-1",
        "timestamp": "2026-11-21T12:00:00.000Z",
        "outcome": "invented",
        "complete": True,
        "interrupted": False,
        "interrupt_signal": None,
        "accounting": {"missing": [], "duplicate": [], "unexpected": []},
    }

    with pytest.raises(EvidenceFormatError, match="outcome"):
        decode_jsonl(json.dumps(record))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", 17),
        ("oom_kill_delta", "two"),
        ("unavailable_reason", 17),
    ],
)
def test_a_cgroup_field_with_a_bad_value_is_refused(
    field: str,
    value: object,
) -> None:
    """An available-looking cgroup object still validates its own fields."""
    cgroup: dict[str, object] = {
        "path": "/sys/fs/cgroup/job",
        "start": None,
        "finish": None,
        "oom_kill_delta": 1,
        "unavailable_reason": None,
    }
    cgroup[field] = value

    with pytest.raises(EvidenceFormatError, match=field.split("_")[0]):
        decode_jsonl(
            json.dumps({
                **_valid_record("harness-finish"),
                "cgroup": cgroup,
            })
        )


def test_an_output_stream_of_the_wrong_shape_is_refused() -> None:
    """A list or object in `stream` is a schema error, not a TypeError."""
    for stream in ([], {}):
        record = _valid_record("output")
        record["stream"] = stream

        with pytest.raises(EvidenceFormatError, match="stream"):
            decode_jsonl(json.dumps(record))


def test_a_harness_finish_record_without_a_finish_time_is_accepted() -> None:
    """The producer emits a null timestamp for a harness that never ran."""
    record = _valid_record("harness-finish")
    record["timestamp"] = None

    (decoded,) = decode_jsonl(json.dumps(record))

    assert decoded["timestamp"] is None


def test_a_not_run_result_round_trips_through_its_finish_record() -> None:
    """The producer and reader agree on a harness that never ran."""
    from rust_prover_tools.supervision.events import harness_finish_record
    from rust_prover_tools.supervision.run_finalize import not_run_result

    record = {
        "schema": 1,
        **harness_finish_record(
            run_id="run-1", result=not_run_result(spec(), "skipped")
        ),
    }

    (decoded,) = decode_jsonl(json.dumps(record))

    assert decoded["status"] == "not-run"
    assert decoded["stop_reason"] is None
    assert decoded["timestamp"] is None


def test_null_output_text_is_refused_and_empty_text_is_kept() -> None:
    """The producer always writes the decoded line, possibly empty."""
    null_record = _valid_record("output")
    null_record["text"] = None
    with pytest.raises(EvidenceFormatError, match="text"):
        decode_jsonl(json.dumps(null_record))

    empty_record = _valid_record("output")
    empty_record["text"] = ""
    (decoded,) = decode_jsonl(json.dumps(empty_record))
    assert decoded["text"] == ""


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
