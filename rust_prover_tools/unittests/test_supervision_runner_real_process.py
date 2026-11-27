"""Witness a live callback failure through the real launcher."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import json
import os
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    HarnessSpec,
    MemorySink,
    OutputLine,
    run_supervised,
    validate_report_document,
)
from rust_prover_tools.supervision.config import DiagnosticSinks
from rust_prover_tools.unittests.supervision_doubles import (
    spec,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    config as _config,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.supervision import (
        LaunchCommand,
    )


def _blocking_child(tmp_path: Path) -> tuple[Path, Path]:
    """Write a child that reports its pid, prints one line, then blocks."""
    script = tmp_path / "blocking.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return script, pid_file


class _FailsOnFirstStdoutLine:
    """Fail the first stdout line, recording whether the child was alive."""

    def __init__(self, pid_file: Path) -> None:
        """Remember where the child wrote its pid."""
        self._pid_file = pid_file
        self.delivered: list[str] = []
        self.alive_at_failure: list[bool] = []

    def __call__(self, item: object) -> None:
        """Fail as a full spool would, while the child is still running."""
        line = typ.cast("typ.Any", item)
        if line.stream != "stdout" or line.text != "first":
            return
        self.delivered.append(line.text)
        pid = int(self._pid_file.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.alive_at_failure.append(False)
        else:
            self.alive_at_failure.append(True)
        msg = "the spool is full"
        raise OSError(msg)


def test_a_live_callback_failure_is_raised_and_the_report_validates(
    tmp_path: Path,
) -> None:
    """A real launcher's evidence failure reaches the caller and still reports.

    The supervisor sees the failure as a monitor fault: it stops and joins the
    attempt, accounts for the unrecorded harnesses, publishes a schema-valid
    report, then re-raises the original failure.
    """
    import sys

    from rust_prover_tools.supervision.clock import AsyncSleeper, SystemClock
    from rust_prover_tools.supervision.kani import KaniAdapter, KaniObserver
    from rust_prover_tools.supervision.launch_command import LaunchCommand
    from rust_prover_tools.supervision.launcher import CuprumHarnessLauncher
    from rust_prover_tools.supervision.run_environment import (
        EvidenceEnvironment,
        ExecutionEnvironment,
        RunEnvironment,
        RunPolicyEnvironment,
    )
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedCgroup as _ScriptedCgroup,
    )
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedProbe as _ScriptedProbe,
    )

    script, pid_file = _blocking_child(tmp_path)
    fail = _FailsOnFirstStdoutLine(pid_file)

    def build(identity: HarnessSpec) -> LaunchCommand:
        """Run the blocking script from the test's directory."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(script)), cwd=tmp_path)

    class _FailingObserver(KaniObserver):
        """A live observer that fails on the child's first stdout line."""

        def observe(self, line: OutputLine) -> None:
            """Record the intended line, fail, and otherwise forward."""
            fail(line)
            super().observe(line)

    class _FailingAdapter(KaniAdapter):
        """The real Kani adapter, with a failing live observer."""

        def observer(self, spec: HarnessSpec) -> KaniObserver:
            """Return the observer that fails on the first stdout line."""
            del spec
            return _FailingObserver()

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
    )
    report_path = tmp_path / "report.json"
    environment = RunEnvironment(
        execution=ExecutionEnvironment(
            launcher=launcher,
            probe=_ScriptedProbe(),
            cgroup=_ScriptedCgroup(),
            sleeper=AsyncSleeper(),
            adapter=_FailingAdapter(version="0.67.0"),
            build=build,
            clock=clock,
        ),
        evidence=EvidenceEnvironment(diagnostics=Diagnostics(jsonl=MemorySink())),
        policy=RunPolicyEnvironment(),
    )
    config = dc.replace(
        _config(
            timeout_seconds=30.0,
            sample_interval_seconds=30.0,
            heartbeat_interval_seconds=30.0,
        ),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    with pytest.raises(OSError, match="the spool is full"):
        asyncio.run(run_supervised((spec(),), config, environment))

    # The failing write came from the child's stdout while that child was
    # alive and blocked, so the launcher stopped a real long-lived process
    # rather than losing a write from one that had already exited.
    assert fail.delivered == ["first"]
    assert fail.alive_at_failure == [True]
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert len(results) == 1
    assert results[0]["harness"] == spec().harness
    assert results[0]["status"] == "interrupted"
    assert results[0]["stop_reason"] == "interrupted"
    assert report_path.exists()
