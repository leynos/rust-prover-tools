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
    record_group,
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
    await record_group(tmp_path, tmp_path / "child.pid")
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
    await record_group(tmp_path, tmp_path / "child.pid")
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


def _start_stubborn_child(tmp_path: Path) -> subprocess.Popen[bytes]:
    """Start a SIGTERM-ignoring child in its own session and record its group."""
    pid_file = tmp_path / "stuck.pid"
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"p = pathlib.Path({str(pid_file)!r})\n"
        "t = p.with_name(p.name + '.tmp')\n"
        "t.write_text(str(os.getpid()))\n"
        "os.replace(t, p)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    # The child must outlive this call so the parent's deadline has something
    # real to stop, so the handle is deliberately not closed here. It gets no
    # inherited stdout or stderr: holding the runner's pipe would keep the
    # parent's read waiting for EOF after the runner itself is gone.
    # pylint: disable-next=consider-using-with
    child = subprocess.Popen(
        [sys.executable, "-I", str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists(), "the stubborn child never started"
    _write_pgid(tmp_path / "pgid", child.pid)
    return child


@_case("stuck_settlement")
async def _case_stuck_settlement(tmp_path: Path) -> None:
    """Hang a settlement that suppresses cancellation forever.

    A synthetic handle's settlement ignores every cancellation, which is the
    condition the helper must bound. This case proves the helper's own
    deadline; the parent's cleanup is proved separately by `deadline_reap`.
    """

    class _StuckHandle:
        """A handle whose settlement suppresses cancellation forever."""

        group_id: int | None = None

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

    try:
        await settle_within(_StuckHandle(), deadline=0.5)
    except AssertionError:
        # The helper has now refused to settle and escalated; publish that
        # fact so the parent can distinguish this path from an early crash.
        marker = tmp_path / "settled.marker"
        temporary = marker.with_name(f"{marker.name}.tmp")
        temporary.write_text("settled", encoding="utf-8")
        temporary.replace(marker)
    else:
        msg = "settle_within returned without reporting the stuck settlement"
        raise AssertionError(msg)
    # Keep the interpreter alive so the runner's own watchdog must end it.
    await asyncio.Event().wait()


@_case("deadline_reap")
async def _case_deadline_reap(tmp_path: Path) -> None:
    """Leave a real child running and never clean it up.

    The child ignores SIGTERM and lives in its own session, so only the
    parent's deadline cleanup can end it. The case records the group and then
    blocks, giving the parent the whole cleanup path to prove.
    """
    _start_stubborn_child(tmp_path)
    await asyncio.Event().wait()
