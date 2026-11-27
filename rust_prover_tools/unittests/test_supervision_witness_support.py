"""Hold the witness settlement helper to its independent deadlines."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.supervision_witness_support import (
    WITNESS_DEADLINE_SECONDS,
    is_running,
    settle_within,
)

#: Repository root, so the isolated subprocess can import the test helpers.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The script that runs one witness case in its own interpreter.
_RUNNER = _REPO_ROOT / "tests" / "supervision_witness_runner.py"


class _StuckHandle:
    """A handle whose settlement suppresses cancellation until released."""

    group_id: int | None = None

    def __init__(self) -> None:
        """Create a handle that has not been released."""
        self.released = asyncio.Event()
        self.stopped = False

    def stop(self, reason: object) -> None:
        """Record the stop without settling."""
        del reason
        self.stopped = True

    async def settle(self) -> None:
        """Wait for the test to release the settlement, ignoring cancellation."""
        try:
            await asyncio.wait_for(self.released.wait(), timeout=60)
        except asyncio.CancelledError:
            await self.released.wait()


def test_a_stuck_settlement_fails_within_its_deadline() -> None:
    """A settlement that never finishes fails promptly instead of hanging."""

    async def scenario() -> None:
        """Require the helper to fail, then let the stuck task finish."""
        handle = _StuckHandle()
        with pytest.raises(AssertionError, match="did not stop"):
            await settle_within(handle, deadline=0.02)
        assert handle.stopped
        handle.released.set()
        # Let the cancelled settle task observe the release before the loop
        # shuts down, so the failure path cannot leave it pending.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_a_settlement_that_finishes_is_not_reported_as_stuck() -> None:
    """An ordinary settlement completes without a failure."""

    class _QuickHandle:
        """A handle that settles immediately."""

        group_id: int | None = None

        def stop(self, reason: object) -> None:
            """Nothing to stop."""

        async def settle(self) -> None:
            """Return immediately."""

    async def scenario() -> None:
        """Await the helper on a quick handle."""
        await settle_within(_QuickHandle(), deadline=1.0)

    asyncio.run(scenario())


def test_a_never_released_settlement_is_bounded_at_process_shutdown(
    tmp_path: Path,
) -> None:
    """The whole scenario, loop shutdown included, exits within its bound.

    The scenario's settle task suppresses cancellation and is never released,
    so `asyncio.run` would gather it forever. `run_bounded`'s watchdog must
    end the process instead.
    """
    script = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "from tests.supervision_witness_support import (\n"
        "    run_bounded, settle_within,\n"
        ")\n"
        "class _Stuck:\n"
        "    group_id = None\n"
        "    async def settle(self):\n"
        "        while True:\n"
        "            try:\n"
        "                await asyncio.Event().wait()\n"
        "            except asyncio.CancelledError:\n"
        "                continue\n"
        "    def stop(self, reason):\n"
        "        pass\n"
        "async def scenario():\n"
        "    await settle_within(_Stuck(), deadline=0.05)\n"
        "run_bounded(scenario(), deadline=2.0)\n"
    )
    started = time.monotonic()

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    assert time.monotonic() - started < 15


def test_a_sigterm_ignoring_child_is_reaped(tmp_path: Path) -> None:
    """A child that ignores SIGTERM is killed and joined within the deadline.

    The scenario runs in its own interpreter, under the witness watchdog and
    inside the parent's longer deadline. A blocked event-loop shutdown can
    therefore never hang pytest, and a failed case still has its real child
    killed and reaped through the PID file the case records.
    """
    pid_file = tmp_path / "child.pid"
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(_RUNNER), "sigterm_reap", str(tmp_path)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=WITNESS_DEADLINE_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _kill_recorded_child(pid_file)
        raise
    if result.returncode != 0:
        _kill_recorded_child(pid_file)
    assert result.returncode == 0, result.stderr
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _kill_recorded_child(pid_file: Path) -> None:
    """Kill a child a failed case recorded, then require its absence."""
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    limit = time.monotonic() + 10
    while time.monotonic() < limit and is_running(pid):
        time.sleep(0.02)
    assert not is_running(pid), "the SIGTERM-ignoring child was not reaped"
