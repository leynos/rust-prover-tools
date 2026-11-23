"""Witness one real process group: a stop reaches descendants and heartbeats fire.

Every other supervision test uses doubles, which cannot show that Cuprum's
owned process group really is terminated or that a silent child really does
produce heartbeats while it runs. This test spawns a small Python child whose
own child outlives it unless the group is signalled, gives the run a budget
shorter than the child's own sleep, and asserts both:

- the grandchild is gone, so the stop reached the whole group; and
- evidence records carry output and at least one heartbeat before the stop.

It uses a real clock because the property being proved is about real time and
real process groups. Every other supervision test avoids sleeping.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import json
import os
import signal
import sys
import time
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.supervision import (
    CgroupV2Reader,
    CuprumHarnessLauncher,
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HumanSink,
    JsonlSink,
    MemorySink,
    ProcfsResourceProbe,
    RunEnvironment,
    RunOutcome,
    StopReason,
    SupervisionConfig,
    SystemClock,
    run_supervised_sync,
)
from rust_prover_tools.supervision.launcher import LaunchCommand
from rust_prover_tools.unittests.supervision_doubles import spec

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision.attempt_state import AttemptEvidence


class _NullObserver:
    """An observer that records nothing; this witness checks process behaviour."""

    last_progress: str | None = None

    def observe(self, line: object) -> None:
        """Discard one line."""


@dc.dataclass(frozen=True, slots=True)
class _WitnessAdapter:
    """A prover adapter wrapping the witness classifier."""

    classify_evidence: cabc.Callable[[AttemptEvidence], HarnessClassification]

    def observer(self, spec: object) -> _NullObserver:
        """Return the null observer for this attempt."""
        del spec
        return _NullObserver()

    def classify(
        self,
        evidence: AttemptEvidence,
        observer: object,
    ) -> HarnessClassification:
        """Classify the attempt with the configured function."""
        del observer
        return self.classify_evidence(evidence)


class _RealSleeper:
    """A sleeper that actually waits on the loop."""

    async def sleep(self, seconds: float) -> None:
        """Wait for `seconds`."""
        import asyncio

        await asyncio.sleep(seconds)


def _is_running(pid: int) -> bool:
    """Whether a process still runs, treating a zombie as finished."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return stat.rsplit(")", 1)[-1].strip()[:1] != "Z"


def _script(tmp_path: Path) -> str:
    """Return a child script that spawns a grandchild and then sleeps."""
    pid_path = tmp_path / "grandchild.pid"
    grandchild = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    return (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}])\n"
        f"limit = time.monotonic() + 10\n"
        f"while not pathlib.Path({str(pid_path)!r}).exists()"
        " and time.monotonic() < limit:\n"
        "    time.sleep(0.01)\n"
        "print('proof is running', flush=True)\n"
        "time.sleep(60)\n"
    )


def _classify(evidence: AttemptEvidence) -> HarnessClassification:
    """Classify a stopped witness as timed out."""
    if evidence.stop_reason is StopReason.TIMEOUT:
        return HarnessClassification(status=ExecutionStatus.TIMED_OUT)
    return HarnessClassification(status=ExecutionStatus.EXIT_NONZERO)


@pytest.mark.timeout(120)
def test_a_real_stop_reaches_the_whole_group_and_heartbeats_fire(
    tmp_path: Path,
) -> None:
    """A timeout terminates descendants, and a silent-then-chatty run reports."""
    pid_path = tmp_path / "grandchild.pid"
    child = _script(tmp_path)
    clock = SystemClock()
    launcher = CuprumHarnessLauncher(clock=clock.now, monotonic=clock.monotonic)
    sink = MemorySink()
    environment = RunEnvironment(
        clock=clock,
        launcher=launcher,
        probe=ProcfsResourceProbe(clock=clock),
        cgroup=CgroupV2Reader(),
        diagnostics=Diagnostics(jsonl=sink),
        build=lambda identity: LaunchCommand((sys.executable, "-I", "-c", child)),
        adapter=_WitnessAdapter(_classify),
        sleeper=_RealSleeper(),
        interrupts=None,
    )
    config = SupervisionConfig(
        timeout_seconds=12.0,
        sample_interval_seconds=0.5,
        heartbeat_interval_seconds=1.0,
        watchdog_threshold_bytes=None,
    )

    try:
        report = run_supervised_sync((spec(),), config, environment, run_id="w" * 16)

        assert report.results[0].stop_reason is StopReason.TIMEOUT
        assert report.outcome is RunOutcome.FAILED
        assert pid_path.exists(), "the grandchild never started"
        pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _is_running(pid):
            time.sleep(0.05)
        assert not _is_running(pid), "a descendant survived the stopped group"

        kinds = [record["record"] for record in sink.records]
        assert "output" in kinds, "the child's output was never recorded"
        assert "heartbeat" in kinds, "a silent run produced no heartbeat"
        assert "sample" in kinds, "a running harness produced no sample"
        assert kinds[-1] == "run-finish", "the run was not finalized"
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="utf-8"))
            if _is_running(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.timeout(120)
def test_the_human_and_jsonl_sinks_agree_on_a_real_run(tmp_path: Path) -> None:
    """One run writes the same records to a file and to the human log."""
    child = "print('hello from the child', flush=True)\n"
    clock = SystemClock()
    jsonl_path = tmp_path / "evidence.jsonl"
    human_path = tmp_path / "human.log"
    launcher = CuprumHarnessLauncher(clock=clock.now, monotonic=clock.monotonic)
    with (
        jsonl_path.open("w", encoding="utf-8") as jsonl_handle,
        human_path.open("w", encoding="utf-8") as human_handle,
    ):
        environment = RunEnvironment(
            clock=clock,
            launcher=launcher,
            probe=ProcfsResourceProbe(clock=clock),
            cgroup=CgroupV2Reader(),
            diagnostics=Diagnostics(
                jsonl=JsonlSink(jsonl_handle, path=jsonl_path),
                human=HumanSink(human_handle),
            ),
            build=lambda identity: LaunchCommand((sys.executable, "-I", "-c", child)),
            adapter=_WitnessAdapter(_passing),
            sleeper=_RealSleeper(),
        )
        run_supervised_sync((spec(),), SupervisionConfig(), environment)

    assert "hello from the child" in human_path.read_text(encoding="utf-8")
    records = [
        json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record.get("record") == "output"
        and "hello from the child" in str(record.get("text"))
        for record in records
    ), "the JSONL output record did not carry the child's payload"


def _passing(evidence: AttemptEvidence) -> HarnessClassification:
    """Classify a clean exit as a pass."""
    if evidence.exit_code == 0:
        return HarnessClassification(status=ExecutionStatus.PASSED, detail="exit zero")
    return HarnessClassification(status=ExecutionStatus.FAILED)


@pytest.mark.timeout(120)
def test_a_newline_free_line_is_bounded_end_to_end(tmp_path: Path) -> None:
    """A real 8 MiB newline-free line cannot size the supervisor's buffer."""
    import asyncio

    line = tmp_path / "line.py"
    line.write_text(
        "import sys\n"
        "sys.stdout.write('x' * (8 * 1024 * 1024))\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('after\\n')\n",
        encoding="utf-8",
    )
    forwarded: list[int] = []

    def build(identity: object) -> LaunchCommand:
        """Run the newline-free script through the real launcher."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(line)), cwd=tmp_path)

    def on_line(item: object) -> None:
        """Record the forwarded line length."""
        forwarded.append(len(typ.cast("typ.Any", item).text))

    async def scenario() -> object:
        """Start the child and wait for it on one loop."""
        clock = SystemClock()
        launcher = CuprumHarnessLauncher(
            clock=clock.now,
            monotonic=clock.monotonic,
            max_line_bytes=8 * 1024,
        )
        running = launcher.start(spec(), build, on_line)
        return await running.wait()

    result = typ.cast("typ.Any", asyncio.run(scenario()))

    assert result.exit_code == 0
    assert forwarded, "the child produced no lines"
    assert max(forwarded) <= 8 * 1024, "a line exceeded the configured bound"
