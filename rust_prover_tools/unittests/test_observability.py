"""Contract tests for the opt-in structured event helper."""

from __future__ import annotations

import logging
import typing as typ

import pytest

from rust_prover_tools.observability import log_event, operation_span

_LOGGER = logging.getLogger("rust_prover_tools.unittests.observability")


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured events captured by `caplog`, in order."""
    return [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]


def test_log_event_carries_a_bounded_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One event is one record whose payload is the `event_data` mapping."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER.name):
        log_event(_LOGGER, "download.complete", asset="asset.bin", bytes=7)

    assert _events(caplog) == [
        {"event": "download.complete", "asset": "asset.bin", "bytes": 7}
    ]


def test_operation_span_correlates_start_and_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The closing event repeats the start fields and adds outcome and latency."""
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER.name),
        operation_span(_LOGGER, "kani.snapshot.install", tag="t1") as span,
    ):
        span.finish("activated", cache="miss")

    start, complete = _events(caplog)
    assert start["event"] == "kani.snapshot.install.start"
    assert start["tag"] == "t1"
    assert complete["event"] == "kani.snapshot.install.complete"
    assert complete["tag"] == "t1"
    assert complete["outcome"] == "activated"
    assert complete["cache"] == "miss"
    assert complete["span_id"] == start["span_id"], "the pair must share an id"
    assert typ.cast("float", complete["elapsed_seconds"]) >= 0.0


def test_concurrent_spans_get_distinct_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two overlapping spans of one operation keep separate identities."""
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER.name),
        operation_span(_LOGGER, "kani.snapshot.install") as outer,
        operation_span(_LOGGER, "kani.snapshot.install") as inner,
    ):
        inner.finish("activated")
        outer.finish("activated")

    start_outer, start_inner, complete_inner, complete_outer = _events(caplog)
    first = start_outer["span_id"]
    second = start_inner["span_id"]
    assert first != second, "concurrent spans must not share an identifier"
    assert complete_inner["span_id"] == second, "the inner span must pair"
    assert complete_outer["span_id"] == first, "the outer span must pair"


def test_a_completion_field_overrides_the_start_field_of_the_same_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeated field name is overridden, not a duplicate-keyword crash."""
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER.name),
        operation_span(_LOGGER, "kani.snapshot.install", tag="start") as span,
    ):
        span.finish("activated", tag="complete")

    start, complete = _events(caplog)
    assert start["tag"] == "start", "the start event lost its own field"
    assert complete["tag"] == "complete", "the completion field did not override"


def test_operation_span_reports_a_failure_without_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure names the exception class, not its path-bearing message."""
    message = "secret path /home/example"
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER.name),
        pytest.raises(ValueError, match="secret path"),
        operation_span(_LOGGER, "kani.snapshot.check"),
    ):
        raise ValueError(message)

    start, failed = _events(caplog)
    assert start["event"] == "kani.snapshot.check.start"
    assert failed["event"] == "kani.snapshot.check.failed"
    assert failed["reason"] == "ValueError"
    assert failed["span_id"] == start["span_id"], "the failure must pair with its start"
    assert "secret path" not in str(failed)
    assert typ.cast("float", failed["elapsed_seconds"]) >= 0.0
