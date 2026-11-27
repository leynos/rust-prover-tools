"""Witness real process groups in isolated subprocesses.

Each witness runs in its own interpreter under an independent wall-clock
deadline: a stuck settlement that ignores cancellation can otherwise keep the
process alive during interpreter shutdown. The parent kills the process group
the case recorded, on a timeout and on any non-zero exit, then requires the
child's process entry to disappear, so a failed witness cannot leave a child
behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.supervision_witness_support import WITNESS_DEADLINE_SECONDS, is_running

_RUNNER = Path(__file__).with_name("supervision_witness_runner.py")


def _start_case(name: str, tmp_path: Path) -> subprocess.Popen[str]:
    """Start one witness case in its own interpreter and session."""
    return subprocess.Popen(
        [sys.executable, "-I", str(_RUNNER), name, str(tmp_path)],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _run_case(
    name: str,
    tmp_path: Path,
    *,
    timeout: float = WITNESS_DEADLINE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one witness case, reaping its group on any failure."""
    return _collect(_start_case(name, tmp_path), tmp_path, timeout)


def _collect(
    process: subprocess.Popen[str],
    tmp_path: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Wait for one case, killing its group if it fails or overruns."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _abandon(process, tmp_path)
        raise subprocess.TimeoutExpired(process.args, timeout) from None
    if process.returncode != 0:
        # A watchdog exit or an assertion failure can leave the case's child
        # running; the parent, not the case, owns that cleanup.
        _reap_recorded_group(tmp_path)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _abandon(process: subprocess.Popen[str], tmp_path: Path) -> None:
    """Stop an overrun case, its recorded group, and this process's pipes.

    A case's child is started in its own session, so killing the runner does
    not reach it, and it still holds the runner's inherited stdout and stderr.
    Reading those pipes to EOF would therefore wait for the very child this
    call is about to kill, so the pipes are closed instead of drained.
    """
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    _reap_recorded_group(tmp_path)


def _abandon_if_running(process: subprocess.Popen[str], tmp_path: Path) -> None:
    """Stop and reap a runner that is still alive after a test's assertions.

    A setup failure or a failed expectation leaves the runner and possibly its
    child running, so every deadline test owns this cleanup exactly as the
    timeout path does.
    """
    if process.returncode is None:
        _abandon(process, tmp_path)


def _reap_recorded_group(tmp_path: Path) -> None:
    """Kill and confirm the process group a failed case recorded."""
    pgid_file = tmp_path / "pgid"
    if not pgid_file.exists():
        return
    pgid = int(pgid_file.read_text(encoding="utf-8"))
    if pgid <= 0:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    _assert_gone(pgid, "the failed case's child still has a process entry")


def _recorded_pid(tmp_path: Path, name: str) -> int:
    """Read a process identifier a case recorded."""
    return int((tmp_path / name).read_text(encoding="utf-8"))


def _assert_gone(pid: int, reason: str) -> None:
    """Require a process entry to disappear, a zombie included.

    A zombie still has a `/proc` entry, so requiring its absence proves the
    entry was waited for rather than merely signalled. The runner is reaped by
    this process, which started it; a child the runner left behind is reaped
    by whatever adopted it, so the host must provide an init or subreaper that
    reaps orphans. A zombie that outlives the whole deadline means no such
    reaper exists, and the failure says so rather than passing a weaker
    liveness check. A zombie seen earlier is only transient: an orphan is a
    zombie for the moment between exiting and its adopter's `wait`.
    """
    deadline = time.monotonic() + 10
    last_state = ""
    while time.monotonic() < deadline and Path("/proc", str(pid)).exists():
        try:
            last_state = (
                Path("/proc", str(pid), "stat")
                .read_text(encoding="utf-8")
                .rsplit(")", 1)[-1]
                .strip()[:1]
            )
        except OSError:
            return
        time.sleep(0.02)
    if Path("/proc", str(pid)).exists():
        suffix = (
            " (an unreaped zombie: no adopter is reaping orphans)"
            if (last_state == "Z")
            else ""
        )
        msg = f"{reason}{suffix}"
        raise AssertionError(msg)


def _wait_for_pid(path: Path, *, timeout: float = 10.0) -> int:
    """Wait for a case to record a process identifier."""
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "the case never recorded a child"
    return int(path.read_text(encoding="utf-8"))


def _wait_for_path(path: Path, *, timeout: float = 10.0) -> None:
    """Require a case to publish a marker shortly after it is started."""
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"the case never published {path.name}"


def test_a_real_stop_reaches_the_whole_group_and_heartbeats_fire(
    tmp_path: Path,
) -> None:
    """A timeout terminates descendants, and a silent-then-chatty run reports."""
    result = _run_case("group_stop", tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_gone(_recorded_pid(tmp_path, "grandchild.pid"), "a descendant survived")


def test_the_human_and_jsonl_sinks_agree_on_a_real_run(tmp_path: Path) -> None:
    """One run writes the same records to a file and to the human log."""
    result = _run_case("sinks", tmp_path)

    assert result.returncode == 0, result.stderr
    human = (tmp_path / "human.log").read_text(encoding="utf-8")
    records = [
        json.loads(line)
        for line in (tmp_path / "evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "hello from the child" in human
    assert any(
        record.get("record") == "output"
        and "hello from the child" in str(record.get("text"))
        for record in records
    ), "the finished run left no output record"


def test_a_newline_free_line_is_bounded_end_to_end(tmp_path: Path) -> None:
    """A real 8 MiB newline-free line cannot size the supervisor's buffer."""
    result = _run_case("newline", tmp_path)

    assert result.returncode == 0, result.stderr


def test_an_output_callback_failure_stops_and_joins_the_child(
    tmp_path: Path,
) -> None:
    """A lost evidence write stops the child and is raised to the caller."""
    result = _run_case("callback_failure", tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_gone(_recorded_pid(tmp_path, "child.pid"), "the child was not joined")


def test_a_cancelled_run_does_not_lose_a_callback_failure(
    tmp_path: Path,
) -> None:
    """A supervisor stop after a callback failure still reports the failure."""
    result = _run_case("cancelled_failure", tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_gone(_recorded_pid(tmp_path, "child.pid"), "the child was not joined")


def test_a_sigterm_ignoring_child_is_stopped_and_reaped(tmp_path: Path) -> None:
    """A child that ignores SIGTERM is still stopped and then reaped."""
    result = _run_case("sigterm_reap", tmp_path)

    assert result.returncode == 0, result.stderr
    pid = _recorded_pid(tmp_path, "child.pid")
    _assert_gone(pid, "the SIGTERM-ignoring child was not reaped")


def test_a_stuck_settlement_cannot_outlive_the_parent_deadline(
    tmp_path: Path,
) -> None:
    """A settlement that ignores cancellation cannot hang the test process.

    This case has no child: it proves that a scenario that suppresses
    cancellation is still ended by the parent's deadline, so the settlement
    itself can never stall pytest. The parent's cleanup of a real child is
    proved separately below.
    """
    process = _start_case("stuck_settlement", tmp_path)
    try:
        _wait_for_path(tmp_path / "settled.marker")
        with pytest.raises(subprocess.TimeoutExpired):
            _collect(process, tmp_path, timeout=5.0)
    finally:
        _abandon_if_running(process, tmp_path)

    assert process.returncode is not None, "the parent did not reap the runner"


def test_a_child_left_running_by_a_hung_witness_is_stopped_and_reaped(
    tmp_path: Path,
) -> None:
    """Only the parent can end this case, so its cleanup path is what is proved.

    The case starts a real SIGTERM-ignoring child and then blocks without ever
    touching it. The parent's deadline is therefore the only thing that can
    stop the child. Termination and reaping are asserted separately: the
    zombie-aware liveness check proves the child stopped, and the disappearance
    of its process entry proves something reaped it. The runner's own collected
    status proves the parent reaped the interpreter it started.
    """
    process = _start_case("deadline_reap", tmp_path)
    try:
        pid = _wait_for_pid(tmp_path / "pgid")
        assert is_running(pid), "the child exited before the parent acted"
        with pytest.raises(subprocess.TimeoutExpired):
            _collect(process, tmp_path, timeout=5.0)
    finally:
        _abandon_if_running(process, tmp_path)

    assert process.returncode is not None, "the parent did not reap the runner"
    assert not is_running(pid), "the parent did not stop the child"
    _assert_gone(pid, "the stopped child was not reaped")
