"""Tests for Cuprum-backed command helpers."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.commands import (
    CommandSpec,
    command_line,
    run_checked,
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
