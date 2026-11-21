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
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

from rust_prover_tools.supervision.sinks import MIN_LINE_BYTES

__all__ = (
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WATCHDOG_THRESHOLD_BYTES",
    "DiagnosticSinks",
    "SupervisionConfig",
    "require_distinct_destinations",
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


def require_distinct_destinations(
    *,
    human_path: Path | None = None,
    jsonl_path: Path | None = None,
    report_path: Path | None = None,
) -> None:
    """Refuse destinations that name one file under two roles.

    Human and JSONL records have different grammars, so appending both to one
    file produces a file that is neither. The report is written atomically and
    would replace whatever evidence already occupies its path. Paths are
    resolved first, so a relative spelling, a symlink, or a symlinked parent
    cannot disguise an alias.

    Parameters
    ----------
    human_path : Path | None
        Human evidence destination, or `None` when it is disabled.
    jsonl_path : Path | None
        Versioned JSONL destination, or `None` when it is disabled.
    report_path : Path | None
        Final report destination, or `None` when it is disabled.

    Raises
    ------
    ValueError
        Two supplied destinations resolve to the same file.
    """
    seen: dict[Path, str] = {}
    for name, path in (
        ("human_path", human_path),
        ("jsonl_path", jsonl_path),
        ("report_path", report_path),
    ):
        if path is None:
            continue
        resolved = path.resolve()
        if resolved in seen:
            msg = (
                "diagnostic destinations must be distinct: "
                f"{seen[resolved]} and {name} both name {resolved}"
            )
            raise ValueError(msg)
        seen[resolved] = name


@dc.dataclass(frozen=True, slots=True)
class DiagnosticSinks:
    """Where live evidence is written.

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

    def __post_init__(self) -> None:
        """Refuse two records that would append to one file."""
        require_distinct_destinations(
            human_path=self.human_path,
            jsonl_path=self.jsonl_path,
            report_path=self.report_path,
        )


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
        if self.tail_bytes <= 0:
            msg = "tail_bytes must be positive"
            raise ValueError(msg)
