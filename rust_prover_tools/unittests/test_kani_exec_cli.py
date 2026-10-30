"""Exact status and diagnostic contracts at the public Kani execution adapter."""

from __future__ import annotations

import io
import typing as typ

import pytest

from rust_prover_tools import kani_exec_cli
from rust_prover_tools.errors import CommandFailedError, ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_release import KaniExecOptions


class _BrokenPipeStream(io.StringIO):
    """A standard-error double whose writes fail as a closed pipe does."""

    def write(self, _text: str) -> int:
        """Fail as a closed pipe does."""
        raise BrokenPipeError


@pytest.mark.parametrize(("status", "expected"), [(0, 0), (23, 23), (-15, 143)])
def test_adapter_preserves_exit_and_signal_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    expected: int,
) -> None:
    """No output or exception is invented for a completed verifier child."""

    def run(_options: KaniExecOptions, _environment: cabc.Mapping[str, str]) -> int:
        return status

    monkeypatch.setattr(kani_exec_cli, "exec_kani", run)
    assert (
        kani_exec_cli.kani_exec(installation="sdk", cwd="proof", extra_arg=["list"])
        == expected
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(("status", "expected"), [(23, 23), (-15, 143)])
def test_adapter_prints_one_command_failure_and_preserves_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    expected: int,
) -> None:
    """Identity-command failures preserve ordinary and signal status mappings."""
    error = CommandFailedError("selected-tool", status, "identity failed")

    def run(_options: KaniExecOptions, _environment: cabc.Mapping[str, str]) -> int:
        raise error

    monkeypatch.setattr(kani_exec_cli, "exec_kani", run)
    assert (
        kani_exec_cli.kani_exec(installation="sdk", cwd="proof", extra_arg=["list"])
        == expected
    )
    assert capsys.readouterr() == ("", f"{error}\n")


def test_adapter_prints_one_typed_validation_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expected validation failures remain actionable CLI errors with status one."""

    def run(_options: KaniExecOptions, _environment: cabc.Mapping[str, str]) -> int:
        msg = "invalid selected release"
        raise ProverToolError(msg)

    monkeypatch.setattr(kani_exec_cli, "exec_kani", run)
    assert (
        kani_exec_cli.kani_exec(installation="sdk", cwd="proof", extra_arg=["list"])
        == 1
    )
    assert capsys.readouterr() == ("", "invalid selected release\n")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CommandFailedError("selected-tool", 23, "identity failed"), 23),
        (ProverToolError("invalid selected release"), 1),
    ],
    ids=("command-failed", "validation-failed"),
)
def test_closed_diagnostic_pipe_preserves_the_computed_status(
    monkeypatch: pytest.MonkeyPatch,
    error: ProverToolError,
    expected: int,
) -> None:
    """A reader that has gone away cannot replace the status with a traceback."""
    monkeypatch.setattr(kani_exec_cli.sys, "stderr", _BrokenPipeStream())

    def run(_options: KaniExecOptions, _environment: cabc.Mapping[str, str]) -> int:
        raise error

    monkeypatch.setattr(kani_exec_cli, "exec_kani", run)

    assert (
        kani_exec_cli.kani_exec(installation="sdk", cwd="proof", extra_arg=["list"])
        == expected
    ), "the diagnostic is expendable; the verifier status is not"
