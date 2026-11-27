"""Real-process witness cases, run as an isolated script.

This module is not collected as a test: it is the script a witness test starts
in its own interpreter. It holds the shared helpers and the first three cases;
``supervision_witness_children_support`` registers the remaining cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncio  # ruff: ignore[module-import-not-at-top-of-file]
import contextlib  # ruff: ignore[module-import-not-at-top-of-file]
import dataclasses as dc  # ruff: ignore[module-import-not-at-top-of-file]
import inspect  # ruff: ignore[module-import-not-at-top-of-file]
import json  # ruff: ignore[module-import-not-at-top-of-file]
import os  # ruff: ignore[module-import-not-at-top-of-file]
import signal  # ruff: ignore[module-import-not-at-top-of-file]
import time  # ruff: ignore[module-import-not-at-top-of-file]
import typing as typ  # ruff: ignore[module-import-not-at-top-of-file]

from rust_prover_tools.supervision import (  # ruff: ignore[module-import-not-at-top-of-file]
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
from rust_prover_tools.supervision.launcher import LaunchCommand  # ruff: ignore[module-import-not-at-top-of-file]
from rust_prover_tools.unittests.supervision_doubles import spec  # ruff: ignore[module-import-not-at-top-of-file]
from tests.supervision_witness_support import (  # ruff: ignore[module-import-not-at-top-of-file]
    SCENARIO_WATCHDOG_SECONDS,
    is_running,
    run_bounded,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence

_CASES: dict[str, cabc.Callable[[Path], object]] = {}


def _case(
    name: str,
) -> cabc.Callable[[cabc.Callable[[Path], object]], cabc.Callable[[Path], object]]:
    """Register one witness case under `name`."""

    def register(
        function: cabc.Callable[[Path], object],
    ) -> cabc.Callable[[Path], object]:
        """Record the case in the dispatch table."""
        _CASES[name] = function
        return function

    return register


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
        await asyncio.sleep(seconds)


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
        "limit = time.monotonic() + 10\n"
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


def _passing(evidence: AttemptEvidence) -> HarnessClassification:
    """Classify a clean exit as a pass."""
    if evidence.exit_code == 0:
        return HarnessClassification(status=ExecutionStatus.PASSED, detail="exit zero")
    return HarnessClassification(status=ExecutionStatus.FAILED)


def _failing_callback_script(tmp_path: Path) -> Path:
    """Write a child that prints two lines and then blocks."""
    script = tmp_path / "blocking.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdout.write('second\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return script


def _write_pgid(path: Path, pid: int) -> None:
    """Record a child's process-group identifier for the parent to reap."""
    path.write_text(str(pid), encoding="utf-8")


@_case("group_stop")
def _case_group_stop(tmp_path: Path) -> None:
    """Terminate descendants on a timeout and report a chatty run."""
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
        while time.monotonic() < deadline and is_running(pid):
            time.sleep(0.05)
        assert not is_running(pid), "a descendant survived the stopped group"

        kinds = [record["record"] for record in sink.records]
        assert "output" in kinds, "the child's output was never recorded"
        assert "heartbeat" in kinds, "a silent run produced no heartbeat"
        assert "sample" in kinds, "a running harness produced no sample"
        assert kinds[-1] == "run-finish", "the run was not finalized"
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="utf-8"))
            if is_running(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@_case("sinks")
def _case_sinks(tmp_path: Path) -> None:
    """Write the same records to a file and to the human log."""
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


@_case("newline")
async def _case_newline(tmp_path: Path) -> None:
    """Bound a real 8 MiB newline-free line to the supervisor's buffer."""
    line = tmp_path / "line.py"
    line.write_text(
        "import sys\n"
        "sys.stdout.write('x' * (8 * 1024 * 1024))\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('after\\n')\n",
        encoding="utf-8",
    )
    forwarded: list[str] = []

    def build(identity: object) -> LaunchCommand:
        """Run the newline-free script through the real launcher."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(line)), cwd=tmp_path)

    def on_line(item: object) -> None:
        """Record the forwarded line text."""
        forwarded.append(typ.cast("typ.Any", item).text)

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
        max_line_bytes=8 * 1024,
    )
    running = launcher.start(spec(), build, on_line)
    result = await running.wait()

    assert result.exit_code == 0
    assert len(forwarded) == 2, f"unexpected forwarded lines: {len(forwarded)}"
    assert max(len(line.encode("utf-8")) for line in forwarded) <= 8 * 1024
    # The clipped line must still arrive, marked, and the line after it must
    # follow: forwarding only `after` would also satisfy a length-only check.
    assert forwarded[0].startswith("x")
    assert "truncated" in forwarded[0]
    assert forwarded[-1] == "after"


def main(name: str, work: Path) -> None:
    """Run one registered case under the watchdog, awaiting an async one."""
    result = _CASES[name](work)
    if inspect.isawaitable(result):
        run_bounded(
            typ.cast("cabc.Coroutine[typ.Any, typ.Any, object]", result),
            deadline=SCENARIO_WATCHDOG_SECONDS,
        )
