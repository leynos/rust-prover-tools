"""Prove the Verus run command reports and forwards a selected solver."""

from __future__ import annotations

import typing as typ

import pytest

import rust_prover_tools.verus_run_cli as run_cli
from rust_prover_tools.z3_selection import (
    CALIBRATED_Z3_BANNER,
    EXPLICIT_SOURCE,
    Z3Selection,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.verus import VerusRunOptions


def test_the_selected_solver_reaches_the_run_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The qualified executable reaches ``run_verus`` and is named on output."""
    binary = tmp_path / "solver"
    binary.write_text("", encoding="utf-8")
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    seen: list[VerusRunOptions] = []
    monkeypatch.setattr(
        run_cli,
        "probe_and_qualify_z3",
        lambda selection, **_: Z3Selection(
            executable=binary,
            banner=CALIBRATED_Z3_BANNER,
            source=EXPLICIT_SOURCE,
        ),
    )
    monkeypatch.setattr(
        run_cli,
        "run_verus",
        lambda options: seen.append(options) or (),
    )

    status = run_cli.verus_run(
        repo_root=tmp_path,
        proof_file=(proof_file,),
        z3=str(binary),
    )
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert seen[0].z3_path == binary, (
        "The command boundary must forward the qualified solver"
    )
    assert f"Solver ({EXPLICIT_SOURCE}): {binary}\n" in captured.out


def test_no_solver_selection_leaves_the_run_options_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a selection, no solver is forwarded and no line is printed."""
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    seen: list[VerusRunOptions] = []
    monkeypatch.setattr(
        run_cli,
        "run_verus",
        lambda options: seen.append(options) or (),
    )

    status = run_cli.verus_run(repo_root=tmp_path, proof_file=(proof_file,))
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert seen[0].z3_path is None, "No selection must leave the environment alone"
    assert "Solver (" not in captured.out, "No solver line belongs in this run"


@pytest.mark.parametrize(
    "extra_arg",
    [["-V", "cvc5"], ["-Vcvc5"]],
    ids=["separate-word", "attached-value"],
)
def test_a_cvc5_override_beside_z3_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_arg: list[str],
) -> None:
    """Refuse a solver override that would make the reported Z3 unused."""
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")

    def refuse_probe(*_args: object, **_kwargs: object) -> object:
        """Fail the test if the run probes a solver it must refuse."""
        pytest.fail("the solver must not be probed for a refused run")

    monkeypatch.setattr(run_cli, "probe_and_qualify_z3", refuse_probe)
    monkeypatch.setattr(
        run_cli,
        "run_verus",
        lambda options: pytest.fail(f"no proof may run: {options}"),
    )

    status = run_cli.verus_run(
        repo_root=tmp_path,
        proof_file=(proof_file,),
        z3="pinned",
        extra_arg=extra_arg,
    )
    captured = capsys.readouterr()

    assert status == 1
    assert "selects the cvc5 solver" in captured.err
    assert "Solver (" not in captured.out


def test_a_cvc5_run_without_a_z3_selection_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CVC5 is a supported choice when no Z3 solver was selected."""
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    seen: list[VerusRunOptions] = []
    monkeypatch.setattr(
        run_cli,
        "run_verus",
        lambda options: seen.append(options) or (),
    )

    status = run_cli.verus_run(
        repo_root=tmp_path,
        proof_file=(proof_file,),
        extra_arg=["-V", "cvc5"],
    )
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert seen[0].z3_path is None
