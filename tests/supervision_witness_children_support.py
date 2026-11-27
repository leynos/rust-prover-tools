"""Child-failure and stuck-settlement witness cases.

These register into the same table as ``supervision_witness_cases`` and are
imported by the isolated runner before dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    CuprumHarnessLauncher,
    StopReason,
    SystemClock,
)
from rust_prover_tools.supervision.launcher import LaunchCommand
from rust_prover_tools.unittests.supervision_doubles import spec
from tests.supervision_witness_cases_support import (
    _case,
    _failing_callback_script,
    _write_pgid,
)
from tests.supervision_witness_support import SETTLE_SECONDS, settle_within

if typ.TYPE_CHECKING:
    from pathlib import Path


@_case("callback_failure")
async def _case_callback_failure(tmp_path: Path) -> None:
    """Stop the child on a lost evidence write and raise it to the caller."""
    script = _failing_callback_script(tmp_path)
    delivered: list[str] = []

    def build(identity: object) -> LaunchCommand:
        """Run the blocking script through the real launcher."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(script)), cwd=tmp_path)

    def refuse(item: object) -> None:
        """Record the delivery and fail as a full spool would."""
        delivered.append(typ.cast("typ.Any", item).text)
        msg = "the spool is full"
        raise OSError(msg)

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
        max_line_bytes=8 * 1024,
    )
    running = launcher.start(spec(), build, refuse)
    _write_pgid(tmp_path / "pgid", typ.cast("int", running.group_id or 0))
    try:
        with pytest.raises(OSError, match="the spool is full"):
            await asyncio.wait_for(running.wait(), timeout=SETTLE_SECONDS)
    finally:
        # A timed-out wait must not leave the blocking child running; the
        # cleanup join is bounded so the test fails rather than hangs.
        running.stop(StopReason.INTERRUPTED)
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await settle_within(running)
    pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    assert delivered == ["first"]


@_case("cancelled_failure")
async def _case_cancelled_failure(tmp_path: Path) -> None:
    """Report a callback failure even after a supervisor stop."""
    script = _failing_callback_script(tmp_path)
    failure_seen = asyncio.Event()

    def build(identity: object) -> LaunchCommand:
        """Run the blocking script through the real launcher."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(script)), cwd=tmp_path)

    def refuse(item: object) -> None:
        """Fail on the first delivered line."""
        del item
        failure_seen.set()
        msg = "the spool is full"
        raise OSError(msg)

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
        max_line_bytes=8 * 1024,
    )
    running = launcher.start(spec(), build, refuse)
    _write_pgid(tmp_path / "pgid", typ.cast("int", running.group_id or 0))
    try:
        await asyncio.wait_for(failure_seen.wait(), timeout=SETTLE_SECONDS)
        running.stop(StopReason.INTERRUPTED)
        with pytest.raises(OSError, match="the spool is full"):
            await settle_within(running)
    finally:
        # The stop and the join are both bounded, so a regression fails
        # promptly instead of leaving a blocking child behind.
        running.stop(StopReason.INTERRUPTED)
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await settle_within(running)


@_case("sigterm_reap")
async def _case_sigterm_reap(tmp_path: Path) -> None:
    """Stop a child that ignores SIGTERM and require it to be reaped."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('ready\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )

    def build(identity: object) -> LaunchCommand:
        """Run the stubborn child through the real launcher."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(script)), cwd=tmp_path)

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(clock=clock.now, monotonic=clock.monotonic)
    handle = launcher.start(spec(), build, lambda line: None)
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert pid_file.exists(), "the stubborn child never started"
    handle.stop(StopReason.TIMEOUT)
    await settle_within(handle, deadline=0.5)
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@_case("stuck_settlement")
async def _case_stuck_settlement(tmp_path: Path) -> None:
    """Run a real child beside a settlement that can never finish.

    The child ignores SIGTERM and runs in its own session, so it survives
    until something kills the group. A synthetic handle's settlement
    suppresses cancellation forever, which is the condition the helper must
    bound. The case records the real child's group and then waits without end,
    so the parent's independent deadline is what ends it.
    """
    pid_file = tmp_path / "stuck.pid"
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    # The child must outlive this call so the parent's deadline has something
    # real to reap, so the handle is deliberately not closed here.
    # pylint: disable-next=consider-using-with
    child = subprocess.Popen(
        [sys.executable, "-I", str(script)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    _write_pgid(tmp_path / "pgid", child.pid)

    class _StuckHandle:
        """A handle whose settlement suppresses cancellation forever."""

        group_id = child.pid

        def stop(self, reason: object) -> None:
            """Record the stop without settling."""
            del reason

        async def settle(self) -> None:
            """Never return, ignoring every cancellation."""
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

    with contextlib.suppress(AssertionError):
        await settle_within(_StuckHandle(), deadline=0.5)
    # The helper gave up; keep the interpreter alive so the parent's own
    # deadline must reap the real child.
    await asyncio.Event().wait()
