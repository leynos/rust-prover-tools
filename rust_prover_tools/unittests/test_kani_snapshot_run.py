"""Contract tests for executing a proof through the active snapshot."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.install import install_snapshot
from rust_prover_tools.kani_snapshot.run import RunOptions, run_snapshot
from tests.kani_snapshot_install_fixtures import (
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallHooks


@pytest.fixture
def installed(tmp_path: Path) -> tuple[RunOptions, InstallHooks]:
    """Install the fixture snapshot and return run options and hooks."""
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    install_snapshot(install_options(case), hooks)
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
    assert result.exit_code == 7, "launcher exit status was not preserved"
    assert result.stdout.splitlines() == [
        "--harness",
        "bounded push",
        "--output-format=terse",
    ], "launcher did not receive the forwarded arguments"


def test_run_returns_a_bounded_tail_of_proof_output(
    installed: tuple[RunOptions, InstallHooks],
) -> None:
    """A verbose proof is mirrored but its capture stays bounded.

    The fixture launcher echoes every forwarded argument and exits 7. Enough
    arguments make its output exceed the retained budget, so the result must
    hold the newest lines, not everything the launcher wrote.
    """
    options, hooks = installed
    args = tuple(f"arg{index:05d}" for index in range(12000))

    result = run_snapshot(
        RunOptions(
            pin=options.pin,
            args=args,
            cache_home=options.cache_home,
        ),
        hooks,
    )

    assert result.exit_code == 7, "launcher exit status was not preserved"
    assert len(result.stdout.encode("utf-8")) <= 64 * 1024, "capture grew unbounded"
    assert "arg11999" in result.stdout, "bounded capture dropped the newest output"
    assert "arg00000" not in result.stdout, "bounded capture retained the oldest output"


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
    case = make_case(tmp_path)
    with pytest.raises(ProverToolError, match=r"not installed"):
        run_snapshot(
            RunOptions(pin=case.pin, cache_home=case.cache_home),
            install_hooks(case),
        )


def test_run_refuses_unprovable_cargo_routing(
    installed: tuple[RunOptions, InstallHooks],
    tmp_path: Path,
) -> None:
    """A package backend the child cannot shadow stops the proof first."""
    options, hooks = installed
    repository = tmp_path / "repository"
    (repository / ".cargo").mkdir(parents=True)
    (repository / ".cargo" / "config.toml").write_text(
        '[profile.dev.package."*"]\ncodegen-backend = "cranelift"\n',
        encoding="utf-8",
    )

    with pytest.raises(ProverToolError, match=r"package"):
        run_snapshot(
            RunOptions(
                pin=options.pin,
                cache_home=options.cache_home,
                cwd=repository,
            ),
            hooks,
        )
