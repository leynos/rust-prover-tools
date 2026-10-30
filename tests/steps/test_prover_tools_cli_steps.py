"""Behavioural tests for the prover tools CLI."""

from __future__ import annotations

import dataclasses as dc
import os
import subprocess
import sys
import typing as typ
from pathlib import Path

from pytest_bdd import given, parsers, scenarios, then, when

from tests.conftest import FakeVerusBinarySpec

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)

scenarios("../features/prover_tools_cli.feature")


def sanitized_subprocess_env() -> dict[str, str]:
    """Return subprocess environment without GitHub Actions input bindings."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("INPUT_")
    }


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


@given(
    "a Verus binary that reports per-file proof verdicts",
    target_fixture="verus_bin",
)
def verus_binary_reports_per_file_verdicts(
    repo_root: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> Path:
    """Create a fake Verus binary that fails only the recorded proof files."""
    verus_bin = repo_root / "bin" / "verus"
    verdict_dir = repo_root / "verdicts"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(
            proof_command=(
                'printf "verified %s\\n" "$1"\n'
                f'verdict="{verdict_dir}/$(basename "$1").exit"\n'
                'if [ -f "$verdict" ]; then\n'
                '  exit "$(cat "$verdict")"\n'
                "fi\n"
                "exit 0"
            ),
        ),
    )
    return verus_bin


@given(parsers.parse('the Verus proof for "{proof_name}" is accepted'))
def verus_proof_is_accepted(repo_root: Path, proof_name: str) -> None:
    """Write a proof file the fake Verus binary will accept."""
    (repo_root / proof_name).write_text("proof fn accepted() {}\n", encoding="utf-8")


@given(
    parsers.parse(
        'the Verus proof for "{proof_name}" is rejected with exit code {exit_code:d}',
    ),
)
def verus_proof_is_rejected(
    repo_root: Path,
    proof_name: str,
    exit_code: int,
) -> None:
    """Write a proof file plus the exit code the fake Verus binary must return."""
    (repo_root / proof_name).write_text("proof fn rejected() {}\n", encoding="utf-8")
    verdict_dir = repo_root / "verdicts"
    verdict_dir.mkdir(exist_ok=True)
    (verdict_dir / f"{proof_name}.exit").write_text(f"{exit_code}\n", encoding="utf-8")


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
        env=sanitized_subprocess_env(),
    )
    return CliRun(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def run_verus_cli(
    repo_root: Path,
    verus_bin: Path,
    proof_files: tuple[Path, ...],
) -> CliRun:
    """Run `verus run` through the public CLI with one flag per proof file."""
    proof_args = [arg for proof in proof_files for arg in ("--proof-file", str(proof))]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--repo-root",
            str(repo_root),
            *proof_args,
            "--verus-bin",
            str(verus_bin),
            "--no-ensure-toolchain",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
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
    return run_verus_cli(repo_root, verus_bin, (proof_file,))


@when(
    parsers.parse(
        'the user runs Verus proofs on "{first_proof}" and "{second_proof}"',
    ),
    target_fixture="cli_run",
)
def user_runs_verus_proofs(
    repo_root: Path,
    verus_bin: Path,
    first_proof: str,
    second_proof: str,
) -> CliRun:
    """Run two proof files through the public CLI in a single invocation."""
    return run_verus_cli(
        repo_root,
        verus_bin,
        (repo_root / first_proof, repo_root / second_proof),
    )


def reported_proof_names(stderr: str, label: str) -> list[str]:
    """Return proof file names listed on the `label` line of stderr.

    The wrapper reports absolute paths, while a scenario names proof files
    relative to the repository root, so compare on file names only.
    """
    prefix = f"{label}: "
    names: list[str] = []
    for line in stderr.splitlines():
        if line.startswith(prefix):
            listed = line.removeprefix(prefix).split(", ")
            names.extend(Path(entry).name for entry in listed)
    return names


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


@then(
    parsers.parse(
        'stdout contains the proof verification result for "{proof_name}"',
    ),
)
def stdout_contains_proof_result_for(cli_run: CliRun, proof_name: str) -> None:
    """Assert the named file's verifier output reached stdout.

    The result must come from that file, so a run that silently skipped it
    cannot satisfy this step.
    """
    verified_lines = [
        line for line in cli_run.stdout.splitlines() if line.startswith("verified ")
    ]
    assert any(line.endswith(proof_name) for line in verified_lines), (
        f"stdout did not report a result for {proof_name!r}: {cli_run.stdout!r}"
    )


@then(parsers.parse('stderr names the failing proof file "{proof_name}"'))
def stderr_names_failing_proof_file(cli_run: CliRun, proof_name: str) -> None:
    """Assert the failure message attributes the failure to the right file."""
    assert reported_proof_names(cli_run.stderr, "Proof file") == [proof_name], (
        f"stderr did not name {proof_name!r} as the failing proof file: "
        f"{cli_run.stderr!r}"
    )


@then(
    parsers.parse(
        'stderr reports the proof file proved before the failure "{proof_name}"',
    ),
)
def stderr_reports_earlier_proved_file(cli_run: CliRun, proof_name: str) -> None:
    """Assert a partial run reports the files it already proved."""
    reported = reported_proof_names(cli_run.stderr, "Proved before the failure")
    assert reported == [proof_name], (
        f"stderr did not report {proof_name!r} as already proved: {cli_run.stderr!r}"
    )


@then(parsers.parse("the command exits with the prover exit code {exit_code:d}"))
def command_exits_with_prover_code(cli_run: CliRun, exit_code: int) -> None:
    """Assert the prover's exit status survives the wrapper."""
    assert cli_run.returncode == exit_code, (
        f"expected prover exit code {exit_code}, got {cli_run.returncode}; "
        f"stdout: {cli_run.stdout}; stderr: {cli_run.stderr}"
    )
