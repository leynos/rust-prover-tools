"""Hold the supervision value types to their construction-time refusals."""

from __future__ import annotations

import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

from rust_prover_tools.supervision import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WATCHDOG_THRESHOLD_BYTES,
    DiagnosticSinks,
    HarnessSpec,
    Measurement,
    SupervisionConfig,
)


def test_an_available_measurement_carries_its_value() -> None:
    """An available measurement reports the value and no reason."""
    measurement = Measurement.available(4096)

    assert measurement.is_available
    assert measurement.value == 4096
    assert measurement.unavailable_reason is None


def test_an_unavailable_measurement_carries_its_reason() -> None:
    """An unavailable measurement names why it could not be taken."""
    measurement = Measurement.unavailable("meminfo is unreadable")

    assert not measurement.is_available
    assert measurement.value is None
    assert measurement.unavailable_reason == "meminfo is unreadable"


def test_a_measurement_needs_exactly_one_of_value_and_reason() -> None:
    """A value without a reason, or a reason without a value, is refused."""
    with pytest.raises(ValueError, match="exactly one"):
        Measurement()
    with pytest.raises(ValueError, match="exactly one"):
        Measurement(value=1, unavailable_reason="both")
    with pytest.raises(ValueError, match="cannot be negative"):
        Measurement.available(-1)


def test_a_harness_identity_needs_a_package_and_name() -> None:
    """An identity with an empty part cannot be selected or accounted for."""
    with pytest.raises(ValueError, match="package and a harness name"):
        HarnessSpec(package="", harness="harnesses::verified")
    with pytest.raises(ValueError, match="package and a harness name"):
        HarnessSpec(package="example", harness="")
    with pytest.raises(ValueError, match="attempt must be positive"):
        HarnessSpec(package="example", harness="harnesses::verified", attempt=0)


def test_a_qualified_identity_names_its_package() -> None:
    """The qualified identity is the accounting key."""
    assert (
        HarnessSpec(package="example", harness="harnesses::verified").qualified
        == "example::harnesses::verified"
    )


def test_the_documented_defaults_are_the_source_plan_settings() -> None:
    """The defaults must stay the source plan's proposed starting settings."""
    assert pytest.approx(1_200.0) == DEFAULT_TIMEOUT_SECONDS
    assert pytest.approx(5.0) == DEFAULT_SAMPLE_INTERVAL_SECONDS
    assert DEFAULT_WATCHDOG_THRESHOLD_BYTES == 1_024 * 1024 * 1024
    config = SupervisionConfig()
    assert config.timeout_seconds == pytest.approx(1_200.0)
    assert config.sample_interval_seconds == pytest.approx(5.0)
    assert config.watchdog_threshold_bytes == 1_024 * 1024 * 1024


def test_a_consumer_can_choose_its_own_policy() -> None:
    """Every documented setting is configurable."""
    config = SupervisionConfig(
        timeout_seconds=30.0,
        sample_interval_seconds=0.5,
        heartbeat_interval_seconds=2.0,
        watchdog_threshold_bytes=None,
        max_line_bytes=1024,
        tail_bytes=2048,
        sinks=DiagnosticSinks(report_path=None),
    )

    assert config.timeout_seconds == pytest.approx(30.0)
    assert config.watchdog_threshold_bytes is None


@pytest.mark.parametrize(
    "build",
    [
        lambda: SupervisionConfig(timeout_seconds=0.0),
        lambda: SupervisionConfig(timeout_seconds=-1.0),
        lambda: SupervisionConfig(sample_interval_seconds=0.0),
        lambda: SupervisionConfig(heartbeat_interval_seconds=0.0),
        lambda: SupervisionConfig(watchdog_threshold_bytes=-1),
        lambda: SupervisionConfig(max_line_bytes=0),
        lambda: SupervisionConfig(tail_bytes=0),
    ],
)
def test_a_policy_that_cannot_work_is_refused(
    build: cabc.Callable[[], SupervisionConfig],
) -> None:
    """A zero interval would hide evidence, so construction refuses it."""
    with pytest.raises(ValueError, match="must"):
        build()


def test_a_disabled_watchdog_is_allowed() -> None:
    """A consumer may run without a host-memory floor."""
    config = SupervisionConfig(watchdog_threshold_bytes=None)
    assert config.watchdog_threshold_bytes is None


def test_the_memory_watchdog_stop_is_not_a_kernel_oom_claim() -> None:
    """The stop reason vocabulary keeps policy apart from kernel evidence."""
    from rust_prover_tools.supervision import StopReason

    assert StopReason.MEMORY_WATCHDOG.value == "memory-watchdog"
    assert {reason.value for reason in StopReason} == {
        "exited",
        "timeout",
        "memory-watchdog",
        "interrupted",
        "launch-failed",
    }


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan")],
)
def test_non_finite_timing_values_are_refused(value: float) -> None:
    """A non-finite budget cannot produce valid JSON or a meaningful deadline."""
    with pytest.raises(ValueError, match="finite"):
        SupervisionConfig(timeout_seconds=value)


def test_the_policy_holds_destinations_without_inspecting_the_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy construction is pure; only an adapter resolves destination identity.

    The alias rule needs `stat()` and `resolve()`, so it lives beside the file
    handles. A policy built from any spellings, even the same path twice, must
    not consult the host filesystem.
    """
    from pathlib import Path as _Path

    def refuse(*args: object, **kwargs: object) -> None:
        """Fail the test if policy construction performs filesystem I/O."""
        del args, kwargs
        msg = "policy construction touched the filesystem"
        raise AssertionError(msg)

    monkeypatch.setattr(_Path, "resolve", refuse)
    monkeypatch.setattr(_Path, "stat", refuse)
    shared = tmp_path / "evidence"

    sinks = DiagnosticSinks(human_path=shared, jsonl_path=shared)

    assert sinks.human_path == shared
    assert sinks.jsonl_path == shared


def test_a_directly_constructed_negative_measurement_is_refused() -> None:
    """The value object, not only its factory, refuses impossible bytes."""
    with pytest.raises(ValueError, match="negative"):
        Measurement(value=-1)


def test_a_boolean_measurement_is_refused() -> None:
    """A bool is not a byte count, even though Python treats it as an int."""
    with pytest.raises(ValueError, match="integer"):
        Measurement(value=True)
