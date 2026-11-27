"""Witness real process groups in isolated subprocesses.

Each witness runs in its own interpreter under an independent wall-clock
deadline: a stuck settlement that ignores cancellation can otherwise keep the
process alive during interpreter shutdown. When a deadline expires the parent
kills the recorded process group and fails.
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


def _run_case(
    name: str,
    tmp_path: Path,
    *,
    timeout: float = WITNESS_DEADLINE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one witness case in its own interpreter, reaping on timeout."""
    try:
        return subprocess.run(
            [sys.executable, "-I", str(_RUNNER), name, str(tmp_path)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _reap_recorded_group(tmp_path)
        raise


def _reap_recorded_group(tmp_path: Path) -> None:
    """Kill and confirm the process group a timed-out case recorded."""
    pgid_file = tmp_path / "pgid"
    if not pgid_file.exists():
        return
    pgid = int(pgid_file.read_text(encoding="utf-8"))
    if pgid <= 0:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    _assert_reaped(pgid, "the timed-out child was not reaped")


def _recorded_pid(tmp_path: Path, name: str) -> int:
    """Read a process identifier a successful case recorded."""
    return int((tmp_path / name).read_text(encoding="utf-8"))


def _assert_reaped(pid: int, reason: str = "the child was not reaped") -> None:
    """Require a recorded process to be gone within a bounded wait."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and is_running(pid):
        time.sleep(0.02)
    assert not is_running(pid), reason


def test_a_real_stop_reaches_the_whole_group_and_heartbeats_fire(
    tmp_path: Path,
) -> None:
    """A timeout terminates descendants, and a silent-then-chatty run reports."""
    result = _run_case("group_stop", tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_reaped(_recorded_pid(tmp_path, "grandchild.pid"))


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
    _assert_reaped(_recorded_pid(tmp_path, "child.pid"))


def test_a_cancelled_run_does_not_lose_a_callback_failure(
    tmp_path: Path,
) -> None:
    """A supervisor stop after a callback failure still reports the failure."""
    result = _run_case("cancelled_failure", tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_reaped(_recorded_pid(tmp_path, "child.pid"))


def test_a_stuck_settlement_with_a_real_child_is_reaped(tmp_path: Path) -> None:
    """A settlement that ignores cancellation is bounded and its child reaped."""
    with pytest.raises(subprocess.TimeoutExpired):
        _run_case("stuck_settlement", tmp_path, timeout=5.0)

    _assert_reaped(
        _recorded_pid(tmp_path, "pgid"),
        "the timed-out child was not reaped",
    )
