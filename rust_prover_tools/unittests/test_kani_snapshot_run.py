"""Contract tests for executing a proof through the active snapshot."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.run import RunOptions, run_snapshot
from rust_prover_tools.unittests.test_kani_snapshot_install import (
    _hooks,
    _make_case,
    _options,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallHooks


@pytest.fixture
def installed(tmp_path: Path) -> tuple[RunOptions, InstallHooks]:
    """Install the fixture snapshot and return run options and hooks."""
    case = _make_case(tmp_path)
    hooks = _hooks(case)
    from rust_prover_tools.kani_snapshot.install import install_snapshot

    install_snapshot(_options(case), hooks)
    return RunOptions(pin=case.pin, cache_home=case.cache_home), hooks


def test_run_forwards_arguments_and_exit_status(
    installed: tuple[RunOptions, InstallHooks],
) -> None:
    """The launcher receives every argument and its status is preserved."""
    options, hooks = installed
    result = run_snapshot(
        RunOptions(
            pin=options.pin,
            args=("--harness", "bounded push", "--output-format=terse"),
            cache_home=options.cache_home,
        ),
        hooks,
    )
    assert result.exit_code == 7
    assert result.stdout.splitlines() == [
        "--harness",
        "bounded push",
        "--output-format=terse",
    ]


def test_run_rejects_a_setup_command(
    installed: tuple[RunOptions, InstallHooks],
) -> None:
    """Forwarded setup would replace the pinned backend."""
    options, hooks = installed
    with pytest.raises(ProverToolError, match=r"setup command"):
        run_snapshot(
            RunOptions(pin=options.pin, args=("setup",), cache_home=options.cache_home),
            hooks,
        )


def test_run_rejects_the_lean_backend(
    installed: tuple[RunOptions, InstallHooks],
) -> None:
    """The Lean backend is outside the snapshot's pinned support."""
    options, hooks = installed
    with pytest.raises(ProverToolError, match=r"Lean"):
        run_snapshot(
            RunOptions(
                pin=options.pin,
                args=("-Z", "lean"),
                cache_home=options.cache_home,
            ),
            hooks,
        )


def test_run_requires_an_installed_snapshot(tmp_path: Path) -> None:
    """Running without a generation is a clear error, not a PATH fallback."""
    case = _make_case(tmp_path)
    with pytest.raises(ProverToolError, match=r"not installed"):
        run_snapshot(
            RunOptions(pin=case.pin, cache_home=case.cache_home),
            _hooks(case),
        )
