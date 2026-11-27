"""Validated policy for supervised harness execution.

The defaults are the Cuprum source plan's proposed starting settings: twenty
minutes per harness, a five-second sampling interval, and a 1,024-MiB
available-memory floor. They are policy, not fact: a consumer with different
hardware, a different package, or a different proof budget supplies its own
values, and nothing in the capability assumes these numbers.

Every value is validated at construction so a mistyped interval fails before a
proof starts rather than silently disabling a watchdog.
"""

from __future__ import annotations

import dataclasses as dc
import math
import typing as typ

from rust_prover_tools.supervision.sinks import MIN_LINE_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WATCHDOG_THRESHOLD_BYTES",
    "DiagnosticSinks",
    "SupervisionConfig",
)

#: Twenty minutes, from the Cuprum source plan's proposed starting setting.
DEFAULT_TIMEOUT_SECONDS: typ.Final[float] = 1_200.0

#: Five seconds, from the Cuprum source plan's proposed starting setting.
DEFAULT_SAMPLE_INTERVAL_SECONDS: typ.Final[float] = 5.0

#: 1,024 MiB, from the Cuprum source plan's proposed starting setting.
DEFAULT_WATCHDOG_THRESHOLD_BYTES: typ.Final[int] = 1_024 * 1024 * 1024

#: Fifteen seconds. A harness that compiles for minutes must still say so.
DEFAULT_HEARTBEAT_SECONDS: typ.Final[float] = 15.0

#: Bound for one human or JSONL diagnostic line, in bytes.
DEFAULT_MAX_LINE_BYTES: typ.Final[int] = 8 * 1024


@dc.dataclass(frozen=True, slots=True)
class DiagnosticSinks:
    """Where live evidence is written.

    The policy records intent only. Every filesystem check, including the
    inode-aware refusal of two roles naming one file, belongs to the adapter
    that resolves and opens these destinations
    (`supervision.destinations.require_distinct_destinations`).

    Attributes
    ----------
    human_path : Path | None
        Append human-readable records here. `None` writes to the runner's
        human stream when one is supplied.
    jsonl_path : Path | None
        Append versioned JSONL records here. `None` disables the JSONL sink
        unless an explicit sink object is supplied to the runner.
    report_path : Path | None
        Write the final JSON report here, atomically.
    """

    human_path: Path | None = None
    jsonl_path: Path | None = None
    report_path: Path | None = None


@dc.dataclass(frozen=True, slots=True)
class SupervisionConfig:
    """Policy for one supervised run.

    Attributes
    ----------
    timeout_seconds : float
        Wall-clock budget per harness. Must be positive.
    sample_interval_seconds : float
        Interval between resource samples. Must be positive.
    heartbeat_interval_seconds : float
        Interval between heartbeats when the prover is silent. Must be
        positive.
    watchdog_threshold_bytes : int | None
        Host available-memory floor. A sample below it stops the harness's
        process group. `None` disables the watchdog.
    max_line_bytes : int
        Bound for one diagnostic record, so a pathological line cannot size
        the run's memory.
    max_log_bytes : int
        Bound for the log a Kani adapter retains for its final parse. A longer
        log is reported as truncated evidence.
    tail_bytes : int
        Bound for the retained tail of each stream.
    sinks : DiagnosticSinks
        Configured destinations.
    """

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    watchdog_threshold_bytes: int | None = DEFAULT_WATCHDOG_THRESHOLD_BYTES
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    max_log_bytes: int = 64 * 1024 * 1024
    tail_bytes: int = 64 * 1024
    sinks: DiagnosticSinks = dc.field(default_factory=DiagnosticSinks)

    def __post_init__(self) -> None:
        """Refuse a policy that could hide evidence or never fire."""
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("sample_interval_seconds", self.sample_interval_seconds),
            ("heartbeat_interval_seconds", self.heartbeat_interval_seconds),
        ):
            if not math.isfinite(value) or not value > 0:
                msg = f"{name} must be finite and positive, got {value!r}"
                raise ValueError(msg)
        if self.watchdog_threshold_bytes is not None and (
            self.watchdog_threshold_bytes < 0
        ):
            msg = "watchdog_threshold_bytes must not be negative"
            raise ValueError(msg)
        if self.max_line_bytes < MIN_LINE_BYTES:
            msg = f"max_line_bytes must be at least {MIN_LINE_BYTES}"
            raise ValueError(msg)
        if self.max_log_bytes < MIN_LINE_BYTES:
            msg = f"max_log_bytes must be at least {MIN_LINE_BYTES}"
            raise ValueError(msg)
        if self.tail_bytes <= 0:
            msg = "tail_bytes must be positive"
            raise ValueError(msg)
