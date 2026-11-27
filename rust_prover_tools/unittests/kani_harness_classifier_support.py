"""Shared corpus loader and evidence builders for the Kani classifier tests."""

from __future__ import annotations

import contextlib
import typing as typ
from pathlib import Path

from rust_prover_tools.supervision import (
    CgroupObservation,
    HarnessClassification,
    HarnessSpec,
    Measurement,
    OutputLine,
    StopReason,
)
from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence
from rust_prover_tools.supervision.kani import (
    KaniAdapter,
    KaniLiveFacts,
    KaniObservation,
    KaniObserver,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision.kani_observer import (
        KaniSpool,
    )

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


def _finish(observer: KaniObserver) -> KaniObservation:
    """Finish an observer, release its spool, and return its observation."""
    observation = observer.finish()
    with contextlib.suppress(OSError, ValueError):
        observer.close()
    return observation


class _RecordingSpool:
    """An in-memory spool that records what was written and whether it closed."""

    def __init__(self) -> None:
        """Start with no lines and nothing released."""
        self.written: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        """Record one piece of the log."""
        self.written.append(text)

    def lines(self) -> cabc.Iterator[str]:
        """Yield the recorded log, without its line terminators."""
        yield from "".join(self.written).splitlines()

    def close(self) -> None:
        """Record that the spool was released."""
        self.closed = True


def _no_spool() -> KaniSpool:
    """Fail as an unwritable temporary directory would."""
    msg = "no spool"
    raise OSError(msg)


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
        _finish(_observer(observed or requested, name)),
    )


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


def _live_facts(
    *,
    selections: tuple[str, ...] = (),
    verdicts: tuple[tuple[str, str], ...] = (),
) -> KaniLiveFacts:
    """Return synthetic live facts, including verdicts the observer never records."""
    return KaniLiveFacts(
        banner_version=None,
        selection_records=selections,
        verdicts=verdicts,
        last_progress=None,
        completed=False,
        no_match_reason=None,
    )
