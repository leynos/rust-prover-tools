"""Exact status and diagnostic contracts at the public Kani execution adapter."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools import kani_exec_cli
from rust_prover_tools.errors import CommandFailedError, ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_release import KaniExecOptions


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
