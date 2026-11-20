"""Prove solver selection resolves a real executable and holds it to the pin."""

from __future__ import annotations

import typing as typ

import pytest

import rust_prover_tools.verus_run_cli as run_cli
import rust_prover_tools.z3_selection as selection_module
from rust_prover_tools.binary_assets import Z3_VERSION
from rust_prover_tools.commands import CommandResult
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.z3_selection import (
    CALIBRATED_Z3_BANNER,
    EXPLICIT_SOURCE,
    PINNED_SOURCE,
    PINNED_Z3,
    qualify_z3,
    resolve_z3,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_the_calibrated_banner_names_the_pinned_release() -> None:
    """The banner the qualifier accepts is the release this package installs."""
    assert f"Z3 version {Z3_VERSION} - 64 bit" == CALIBRATED_Z3_BANNER


def test_the_pinned_selection_resolves_to_the_installed_binary(
    tmp_path: Path,
) -> None:
    """``pinned`` names the binary the installer writes below its directory."""
    install_dir = tmp_path / "z3"
    install_dir.mkdir()
    binary = install_dir / "z3"
    binary.write_text("", encoding="utf-8")

    resolved = resolve_z3(PINNED_Z3, repo_root=tmp_path, install_dir=install_dir)

    assert resolved == binary


def test_the_pinned_selection_refuses_a_missing_installation(
    tmp_path: Path,
) -> None:
    """An absent pinned solver is reported rather than silently replaced."""
    with pytest.raises(ProverToolError, match="is not installed at"):
        resolve_z3(PINNED_Z3, repo_root=tmp_path)


def test_an_explicit_file_is_resolved_as_given(tmp_path: Path) -> None:
    """A caller naming an executable gets exactly that executable."""
    binary = tmp_path / "solver"
    binary.write_text("", encoding="utf-8")

    assert resolve_z3(str(binary), repo_root=tmp_path) == binary.resolve()


def test_a_command_name_is_looked_up_on_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare command name is resolved the way the shell would resolve it."""
    located = tmp_path / "bin" / "z3"
    located.parent.mkdir()
    located.write_text("", encoding="utf-8")
    monkeypatch.setattr(selection_module.shutil, "which", lambda _name: str(located))

    assert resolve_z3("z3", repo_root=tmp_path) == located.resolve()


def test_an_unknown_command_name_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that resolves nowhere names no solver."""
    monkeypatch.setattr(selection_module.shutil, "which", lambda _name: None)

    with pytest.raises(ProverToolError, match="names no executable"):
        resolve_z3("definitely-not-a-solver", repo_root=tmp_path)


def test_a_matching_banner_is_qualified(tmp_path: Path) -> None:
    """The reported banner is kept exactly as the solver printed it."""
    binary = tmp_path / "z3"
    binary.write_text("", encoding="utf-8")

    qualified = qualify_z3(
        binary,
        exit_code=0,
        reported=f"{CALIBRATED_Z3_BANNER}\n",
        source=PINNED_SOURCE,
    )

    assert qualified.executable == binary.resolve()
    assert qualified.banner == CALIBRATED_Z3_BANNER
    assert qualified.source == PINNED_SOURCE


def test_a_different_release_is_refused(tmp_path: Path) -> None:
    """A solver that answers with another release is not the pinned solver."""
    binary = tmp_path / "z3"

    with pytest.raises(ProverToolError, match="must report") as exc_info:
        qualify_z3(
            binary,
            exit_code=0,
            reported="Z3 version 4.15.0 - 64 bit",
            source=EXPLICIT_SOURCE,
        )

    message = str(exc_info.value)
    assert CALIBRATED_Z3_BANNER in message
    assert "Z3 version 4.15.0 - 64 bit" in message


def test_a_failed_probe_is_refused_even_with_the_right_banner(
    tmp_path: Path,
) -> None:
    """A status the solver did not succeed with disqualifies it."""
    with pytest.raises(ProverToolError, match="must report"):
        qualify_z3(
            tmp_path / "z3",
            exit_code=1,
            reported=CALIBRATED_Z3_BANNER,
            source=PINNED_SOURCE,
        )


def test_a_silent_solver_is_refused(tmp_path: Path) -> None:
    """No banner at all is reported as no version rather than an empty quote."""
    with pytest.raises(ProverToolError, match="no version"):
        qualify_z3(
            tmp_path / "z3",
            exit_code=0,
            reported="\n",
            source=PINNED_SOURCE,
        )


def _probe_returning(result: CommandResult) -> object:
    """Return a ``run_command`` stub that reports one observed version."""

    def fake_run_command(_spec: object) -> CommandResult:
        """Report the configured probe result."""
        return result

    return fake_run_command


def test_probing_the_pinned_selection_records_that_it_was_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The qualified selection carries where the solver came from."""
    install_dir = tmp_path / "z3"
    install_dir.mkdir()
    binary = install_dir / "z3"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        run_cli,
        "run_command",
        _probe_returning(
            CommandResult(
                stdout=f"{CALIBRATED_Z3_BANNER}\n",
                stderr="",
                exit_code=0,
            ),
        ),
    )

    qualified = run_cli.probe_and_qualify_z3(
        PINNED_Z3,
        repo_root=tmp_path,
        install_dir=install_dir,
    )

    assert qualified.source == PINNED_SOURCE
    assert qualified.executable == binary.resolve()


def test_probing_an_explicit_selection_records_that_it_was_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A solver the caller named is recorded as their choice."""
    binary = tmp_path / "solver"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        run_cli,
        "run_command",
        _probe_returning(
            CommandResult(stdout=CALIBRATED_Z3_BANNER, stderr="", exit_code=0),
        ),
    )

    qualified = run_cli.probe_and_qualify_z3(str(binary), repo_root=tmp_path)

    assert qualified.source == EXPLICIT_SOURCE


def test_probing_refuses_a_solver_that_reports_another_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe result, not the caller's confidence, decides qualification."""
    binary = tmp_path / "solver"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        run_cli,
        "run_command",
        _probe_returning(
            CommandResult(stdout="Z3 version 4.15.0 - 64 bit", stderr="", exit_code=0),
        ),
    )

    with pytest.raises(ProverToolError, match="must report"):
        run_cli.probe_and_qualify_z3(str(binary), repo_root=tmp_path)
