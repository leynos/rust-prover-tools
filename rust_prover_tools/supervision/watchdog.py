"""Decide whether a sample crosses a configured limit.

The deadline and the watchdog are separate questions with one answer: either
the supervisor keeps waiting or it stops the child for a reason it can name.
Keeping the decision pure means every branch is testable without a clock, a
process, or a sleep, and the runner only has to apply the answer.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.models import Measurement, StopReason

__all__ = ("SampleVerdict", "deadline_verdict", "watchdog_verdict")


@dc.dataclass(frozen=True, slots=True)
class SampleVerdict:
    """The stop decision for one monitor tick.

    Attributes
    ----------
    reason : StopReason | None
        The stop to request, or `None` to keep waiting.
    detail : str
        Bounded explanation recorded in the report.
    """

    reason: StopReason | None
    detail: str = ""


def deadline_verdict(
    *,
    elapsed_seconds: float,
    timeout_seconds: float,
) -> SampleVerdict:
    """Return whether the harness exceeded its wall-clock budget.

    Parameters
    ----------
    elapsed_seconds : float
        Seconds since the invocation started.
    timeout_seconds : float
        Configured per-harness budget.

    Returns
    -------
    SampleVerdict
        A timeout stop when the budget is spent, otherwise no stop.
    """
    if elapsed_seconds >= timeout_seconds:
        return SampleVerdict(
            StopReason.TIMEOUT,
            f"harness exceeded its {timeout_seconds:g}-second budget",
        )
    return SampleVerdict(None)


def watchdog_verdict(
    *,
    host_available: Measurement,
    threshold_bytes: int | None,
) -> SampleVerdict:
    """Return whether host available memory fell below the configured floor.

    Parameters
    ----------
    host_available : Measurement
        Sampled host ``MemAvailable``.
    threshold_bytes : int | None
        Floor below which the harness is stopped, or `None` to disable the
        watchdog.

    Returns
    -------
    SampleVerdict
        A watchdog stop when the floor was crossed, otherwise no stop. An
        unavailable measurement never fires: a value that could not be read is
        not evidence of pressure.
    """
    if threshold_bytes is None or host_available.value is None:
        return SampleVerdict(None)
    if host_available.value < threshold_bytes:
        mib = threshold_bytes / (1024 * 1024)
        observed = host_available.value / (1024 * 1024)
        return SampleVerdict(
            StopReason.MEMORY_WATCHDOG,
            f"host available memory fell to {observed:.1f} MiB, "
            f"below the {mib:.0f} MiB floor",
        )
    return SampleVerdict(None)


def first_stop(*verdicts: SampleVerdict) -> SampleVerdict:
    """Return the first verdict that requests a stop, in the given order.

    Parameters
    ----------
    *verdicts : SampleVerdict
        Verdicts in precedence order.

    Returns
    -------
    SampleVerdict
        The first stop, or a no-stop verdict when none applies.
    """
    for verdict in verdicts:
        if verdict.reason is not None:
            return verdict
    return SampleVerdict(typ.cast("StopReason | None", None))
