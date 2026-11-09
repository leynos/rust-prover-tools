"""Tests for Cuprum-backed command helpers."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
import typing as typ
from pathlib import Path

import cuprum
import pytest

from rust_prover_tools.commands import (
    CommandSpec,
    command_line,
    run_checked,
    run_command,
    split_command,
)
from rust_prover_tools.errors import CommandFailedError, ProverToolError

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)


def test_split_command_preserves_quoted_arguments() -> None:
    """Command overrides use shell-like quoting without invoking a shell."""
    assert split_command('cargo kani "--output-format=json"') == (
        "cargo",
        "kani",
        "--output-format=json",
    ), "Should preserve quoted arguments without shell expansion"


def test_split_command_rejects_empty_command() -> None:
    """Empty command overrides fail before command execution."""
    with pytest.raises(ProverToolError, match="command is empty"):
        split_command("   ")


def test_command_line_quotes_special_characters() -> None:
    """Command display quotes shell-sensitive arguments."""
    assert command_line(("cargo", "kani", "space value")) == (
        "cargo kani 'space value'"
    ), "Should quote arguments containing spaces"


def test_run_checked_uses_cmd_mox_shim(cmd_mox: CmdMox) -> None:
    """Command execution goes through PATH and can be intercepted by CmdMox."""
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stdout="kani 0.61.0\n",
    )

    result = run_checked(CommandSpec(("cargo", "kani", "--version")))

    assert result.stdout == "kani 0.61.0\n", "Should capture mocked stdout"


def test_run_checked_passes_environment_to_command(cmd_mox: CmdMox) -> None:
    """Command execution passes environment overlays through Cuprum."""
    cmd_mox.mock("envcheck").with_args().with_env({"RPT_SENTINEL": "1"}).returns(
        stdout="ok\n",
    )

    result = run_checked(CommandSpec(("envcheck",), env={"RPT_SENTINEL": "1"}))

    assert result.stdout == "ok\n", "Command should receive environment overlay"


def test_run_checked_echoes_output_when_requested(
    cmd_mox: CmdMox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Command echo mode tees captured output to stdout."""
    cmd_mox.mock("echoer").with_args().returns(stdout="visible\n")

    run_checked(CommandSpec(("echoer",), echo=True))

    assert "visible\n" in capsys.readouterr().out, "Echo mode should display output"


def test_run_checked_raises_on_non_zero_exit(cmd_mox: CmdMox) -> None:
    """Non-zero command exits become user-facing command failures."""
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stderr="boom\n",
        exit_code=2,
    )

    expected = "command failed \\(2\\): cargo kani --version: boom"
    with pytest.raises(CommandFailedError, match=expected):
        run_checked(CommandSpec(("cargo", "kani", "--version")))


def test_run_command_enforces_a_timeout() -> None:
    """A command that outlives its budget fails instead of hanging."""
    started = time.monotonic()
    with pytest.raises(ProverToolError, match=r"timed out after 0\.2 seconds"):
        run_command(
            CommandSpec(
                (sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                timeout_seconds=0.2,
            )
        )
    assert time.monotonic() - started < 10, "timeout did not interrupt the command"


def _run_with_budget(
    script: str,
    scope: typ.Literal["explicit", "scoped"],
    seconds: float,
) -> None:
    """Run one script under an explicit or scope-supplied budget."""
    argv = (sys.executable, "-I", "-c", script)
    if scope == "explicit":
        run_command(CommandSpec(argv, timeout_seconds=seconds))
        return
    with cuprum.scoped(cuprum.ScopeConfig(timeout=seconds)):
        run_command(CommandSpec(argv))


def _is_running(pid: int) -> bool:
    """Return whether a process still runs, treating a zombie as finished."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # Everything after the last `)` starts with the process state character.
    return stat.rsplit(")", 1)[-1].strip()[:1] != "Z"


@pytest.mark.parametrize("scope", ["explicit", "scoped"])
@pytest.mark.timeout(60)
def test_run_command_terminates_a_timed_out_process_tree(
    tmp_path: Path,
    scope: typ.Literal["explicit", "scoped"],
) -> None:
    """A timed-out command's descendants are terminated with it."""
    started = tmp_path / "started"
    pid_path = tmp_path / "pid"
    grandchild = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        "time.sleep(60)"
    )
    # The child waits until the grandchild has definitely started, so the
    # assertions below cannot pass merely because a survivor had not been
    # scheduled yet.
    child = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}])\n"
        "limit = time.monotonic() + 1.5\n"
        f"while not pathlib.Path({str(started)!r}).exists()"
        " and time.monotonic() < limit:\n"
        "    time.sleep(0.02)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(ProverToolError, match=r"timed out after 2\.5 seconds"):
        _run_with_budget(child, scope, 2.5)
    assert started.exists(), "the grandchild did not start before the timeout"
    pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _is_running(pid):
            time.sleep(0.05)
        assert not _is_running(pid), "a descendant survived the timed-out command"
    finally:
        if _is_running(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
