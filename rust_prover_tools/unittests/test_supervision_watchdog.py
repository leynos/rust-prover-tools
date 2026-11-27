"""Hold the limit decisions to the first applicable stop."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.supervision import (
    Measurement,
    StopReason,
    deadline_verdict,
    watchdog_verdict,
)
from rust_prover_tools.supervision.watchdog import SampleVerdict, first_stop

_MIB = 1024 * 1024


def test_the_deadline_is_not_reached_before_the_budget() -> None:
    """A harness inside its budget keeps running."""
    verdict = deadline_verdict(elapsed_seconds=1199.0, timeout_seconds=1200.0)
    assert verdict.reason is None


def test_the_deadline_is_reached_at_the_budget() -> None:
    """A harness exactly at its budget is stopped."""
    verdict = deadline_verdict(elapsed_seconds=1200.0, timeout_seconds=1200.0)

    assert verdict.reason is StopReason.TIMEOUT
    assert "1200-second budget" in verdict.detail


def test_the_watchdog_is_disabled_by_a_none_threshold() -> None:
    """A consumer may choose no host-memory floor."""
    verdict = watchdog_verdict(
        host_available=Measurement.available(1),
        threshold_bytes=None,
    )

    assert verdict.reason is None


def test_an_unavailable_measurement_never_fires_the_watchdog() -> None:
    """A value that could not be read is not evidence of pressure."""
    verdict = watchdog_verdict(
        host_available=Measurement.unavailable("meminfo unreadable"),
        threshold_bytes=1024 * _MIB,
    )

    assert verdict.reason is None


def test_available_memory_below_the_floor_stops_the_harness() -> None:
    """The watchdog turns host pressure into an attributable policy stop."""
    verdict = watchdog_verdict(
        host_available=Measurement.available(512 * _MIB),
        threshold_bytes=1024 * _MIB,
    )

    assert verdict.reason is StopReason.MEMORY_WATCHDOG
    assert "512.0 MiB" in verdict.detail
    assert "1024 MiB floor" in verdict.detail


def test_available_memory_above_the_floor_keeps_running() -> None:
    """A host with headroom does not trigger the watchdog."""
    verdict = watchdog_verdict(
        host_available=Measurement.available(2048 * _MIB),
        threshold_bytes=1024 * _MIB,
    )

    assert verdict.reason is None


def test_the_first_stop_wins_in_the_given_order() -> None:
    """A timeout that fires first is not replaced by a later watchdog stop."""
    timeout = deadline_verdict(elapsed_seconds=10.0, timeout_seconds=10.0)
    watchdog = watchdog_verdict(
        host_available=Measurement.available(1),
        threshold_bytes=1024 * _MIB,
    )

    assert first_stop(timeout, watchdog).reason is StopReason.TIMEOUT
    assert first_stop(watchdog, timeout).reason is StopReason.MEMORY_WATCHDOG


@pytest.mark.parametrize("elapsed", [0.0, 0.5, 9.99])
def test_no_stop_before_any_limit(elapsed: float) -> None:
    """No limit is reported before one is crossed."""
    verdict = deadline_verdict(elapsed_seconds=elapsed, timeout_seconds=10.0)
    assert verdict.reason is None


_STOP_REASONS = (
    StopReason.TIMEOUT,
    StopReason.MEMORY_WATCHDOG,
    StopReason.INTERRUPTED,
)


@given(st.lists(st.sampled_from((*_STOP_REASONS, None)), max_size=6))
@settings(max_examples=50, deadline=None)
def test_first_stop_returns_the_first_stopping_verdict(
    reasons: list[StopReason | None],
) -> None:
    """Generated verdict sequences select the first stop in argument order."""
    verdicts = [
        SampleVerdict(reason, detail=f"verdict {index}")
        for index, reason in enumerate(reasons)
    ]

    chosen = first_stop(*verdicts)

    first = next(
        (verdict for verdict in verdicts if verdict.reason is not None),
        None,
    )
    if first is None:
        assert chosen.reason is None
        assert chosen.detail == ""
    else:
        assert chosen is first


@given(
    value=st.integers(min_value=0, max_value=1 << 40),
    threshold=st.integers(min_value=0, max_value=1 << 40),
)
@settings(max_examples=50, deadline=None)
def test_the_watchdog_fires_exactly_below_the_floor(
    value: int,
    threshold: int,
) -> None:
    """A generated measurement stops the harness exactly when below the floor."""
    verdict = watchdog_verdict(
        host_available=Measurement.available(value),
        threshold_bytes=threshold,
    )

    assert (verdict.reason is StopReason.MEMORY_WATCHDOG) == (value < threshold)


@given(st.integers(min_value=0, max_value=1 << 40))
@settings(max_examples=50, deadline=None)
def test_an_unavailable_measurement_never_fires_at_any_floor(
    threshold: int,
) -> None:
    """A value that could not be read is not evidence at any configured floor."""
    verdict = watchdog_verdict(
        host_available=Measurement.unavailable("meminfo unreadable"),
        threshold_bytes=threshold,
    )

    assert verdict.reason is None
