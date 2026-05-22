"""Behavioural tests for the prover tools CLI."""

from __future__ import annotations

import dataclasses as dc
import os
import subprocess
import sys
import typing as typ

from pytest_bdd import given, parsers, scenarios, then, when

from tests.conftest import FakeVerusBinarySpec

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)

scenarios("../features/prover_tools_cli.feature")


@dc.dataclass(slots=True, frozen=True)
class CliRun:
    """Captured command-line run."""

    stdout: str
    stderr: str
    returncode: int


@given(
    parsers.parse('a repository with Kani version "{version}"'),
    target_fixture="repo_root",
)
def repo_root_with_kani_version(tmp_path: Path, version: str) -> Path:
    """Create a repository-like directory with a Kani version pin."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f"{version}\n", encoding="utf-8")
    return tmp_path


@given(
    parsers.parse('a repository with Verus version "{version}"'),
    target_fixture="repo_root",
)
def repo_root_with_verus_version(tmp_path: Path, version: str) -> Path:
    """Create a repository-like directory with a Verus version pin."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f"{version}\n", encoding="utf-8")
    return tmp_path


@given(
    parsers.parse('the Kani command reports version "{version}"'),
    target_fixture="reported_kani_version",
)
def kani_command_reports_version(cmd_mox: CmdMox, version: str) -> str:
    """Mock the Kani version command."""
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stdout=f"kani {version}\n",
    )
    return version


@given("the Verus toolchain is installed", target_fixture="verus_bin")
def verus_toolchain_is_installed(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> Path:
    """Create a fake Verus binary whose toolchain probe succeeds."""
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(verus_bin)
    return verus_bin


@given("the Verus toolchain is not installed", target_fixture="verus_bin")
def verus_toolchain_is_not_installed(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> Path:
    """Create a fake Verus binary whose toolchain probe fails."""
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(
            version_exit_code=1,
            version_stderr="required rust toolchain nightly-missing",
            proof_command="printf 'proof should not run\\n'",
        ),
    )
    return verus_bin


@when("the user checks the Kani version", target_fixture="cli_run")
def user_checks_kani_version(repo_root: Path) -> CliRun:
    """Run the Kani version check through the public CLI."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "kani",
            "check-version",
            "--repo-root",
            str(repo_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return CliRun(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


@when(
    parsers.parse('the user runs a Verus proof on "{proof_name}"'),
    target_fixture="cli_run",
)
def user_runs_verus_proof(repo_root: Path, verus_bin: Path, proof_name: str) -> CliRun:
    """Run Verus proof checking through the public CLI."""
    proof_file = repo_root / proof_name
    proof_file.write_text("proof fn example() {}\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--repo-root",
            str(repo_root),
            "--proof-file",
            str(proof_file),
            "--verus-bin",
            str(verus_bin),
            "--no-ensure-toolchain",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return CliRun(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


@then("the command succeeds")
def command_succeeds(cli_run: CliRun) -> None:
    """Assert that the CLI succeeded."""
    assert cli_run.returncode == 0, (
        f"expected zero returncode, got {cli_run.returncode}; "
        f"stdout: {cli_run.stdout}; stderr: {cli_run.stderr}"
    )


@then("the command fails")
def command_fails(cli_run: CliRun) -> None:
    """Assert that the CLI failed."""
    assert cli_run.returncode != 0, (
        f"expected non-zero returncode, got {cli_run.returncode}; "
        f"stdout: {cli_run.stdout}; stderr: {cli_run.stderr}"
    )


@then("stdout contains the matching Kani version message")
def stdout_contains_matching_kani_message(cli_run: CliRun, repo_root: Path) -> None:
    """Assert the success output is user-facing and stable."""
    version_file = repo_root / "tools" / "kani" / "VERSION"
    expected_version = version_file.read_text(encoding="utf-8").strip()
    expected = f"Kani {expected_version} matches from {version_file}.\n"
    assert cli_run.stdout == expected, (
        f"expected stdout {expected!r}, got {cli_run.stdout!r}"
    )


@then("stderr contains the Kani version mismatch message")
def stderr_contains_kani_version_mismatch(
    cli_run: CliRun,
    repo_root: Path,
    reported_kani_version: str,
) -> None:
    """Assert the mismatch output names expected and actual versions."""
    version_file = repo_root / "tools" / "kani" / "VERSION"
    expected_version = version_file.read_text(encoding="utf-8").strip()
    assert (
        f"expected Kani {expected_version} from {version_file}, "
        f"found {reported_kani_version}" in cli_run.stderr
    ), f"stderr did not contain Kani mismatch: {cli_run.stderr!r}"


@then("stdout contains the proof verification result")
def stdout_contains_proof_verification_result(cli_run: CliRun) -> None:
    """Assert the proof command output is streamed to stdout."""
    assert "proof verified\n" in cli_run.stdout, (
        f"stdout did not contain proof result: {cli_run.stdout!r}"
    )


@then("stderr contains the missing toolchain message")
def stderr_contains_missing_toolchain_message(cli_run: CliRun) -> None:
    """Assert a missing Verus Rust toolchain is explained to the user."""
    assert "required rust toolchain nightly-missing" in cli_run.stderr, (
        f"stderr did not contain missing toolchain message: {cli_run.stderr!r}"
    )
