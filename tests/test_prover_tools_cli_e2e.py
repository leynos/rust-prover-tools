"""End-to-end tests for externally observable CLI workflows."""

from __future__ import annotations

import os
import subprocess
import sys
import typing as typ

import pytest

from rust_prover_tools.binary_assets import Z3_VERSION
from tests.conftest import FakeVerusBinarySpec
from tests.support import (
    CHILD_TIMEOUT,
    PROOF_LINES,
    chatty_proof_repo,
    fake_verus_argv,
    sanitized_subprocess_env,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def test_kani_install_compile_is_an_opt_in_cli_fallback(tmp_path: Path) -> None:
    """The public CLI compiles Kani only when --compile is requested."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cargo_log = tmp_path / "cargo.log"
    cargo = tmp_path / "bin" / "cargo"
    cargo.parent.mkdir()
    cargo.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$CARGO_LOG"\n'
        'if [ "$1" = "kani" ] && [ "$2" = "--version" ]; then\n'
        "  printf 'kani 0.61.0\\n'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    environment = sanitized_subprocess_env()
    environment["PATH"] = os.pathsep.join(
        (str(cargo.parent), os.environ.get("PATH", "")),
    )
    environment["CARGO_LOG"] = str(cargo_log)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "kani",
            "install",
            "--repo-root",
            str(tmp_path),
            "--compile",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert "Installing kani-verifier 0.61.0" in completed.stdout, (
        f"the installer banner is missing; stdout: {completed.stdout}"
    )
    assert "kani 0.61.0" in completed.stdout, (
        f"the verified version output is missing; stdout: {completed.stdout}"
    )
    assert cargo_log.read_text(encoding="utf-8").splitlines() == [
        "install --locked kani-verifier --version 0.61.0",
        "kani setup",
        "kani --version",
    ], "the compile fallback should run the expected Cargo sequence"


def test_z3_install_cli_reuses_a_marked_installation(tmp_path: Path) -> None:
    """The public CLI reports an existing marked Z3 installation."""
    install_dir = tmp_path / ".cache" / "z3" / Z3_VERSION
    install_dir.mkdir(parents=True)
    binary = install_dir / "z3"
    binary.write_bytes(b"z3-binary")
    binary.chmod(0o755)
    (install_dir / ".z3-version").write_text(f"{Z3_VERSION}\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "z3",
            "install",
            "--repo-root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert f"Z3 {Z3_VERSION} already installed at {binary}" in completed.stdout, (
        f"the reuse status line is missing; stdout: {completed.stdout}"
    )
    assert f"Export VERUS_Z3_PATH={binary}" in completed.stdout, (
        f"the export instruction is missing; stdout: {completed.stdout}"
    )


def test_z3_install_cli_reports_installation_errors(tmp_path: Path) -> None:
    """A filesystem failure becomes a clean CLI error, not a traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "z3",
            "install",
            "--repo-root",
            str(tmp_path),
            "--install-dir",
            str(blocker / "z3-install"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )

    assert completed.returncode == 1, (
        f"expected exit status 1, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert "failed to inspect Z3 installation" in completed.stderr, (
        f"the installation error is missing; stderr: {completed.stderr}"
    )
    assert "Traceback" not in completed.stderr, (
        f"the CLI must not leak a traceback; stderr: {completed.stderr}"
    )


def test_verus_run_cli_accepts_indented_toolchain_output(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """The public CLI accepts official indented Verus toolchain output."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(
            version_stdout=(
                "Verus 0.2025.01.01\\n"
                "  Toolchain: nightly-2025-01-01-x86_64-unknown-linux-gnu "
                "(overridden by environment variable RUSTUP_TOOLCHAIN)\\n"
            ),
            proof_command="printf 'verified %s\\n' \"$1\"",
        ),
    )

    completed = subprocess.run(
        fake_verus_argv(tmp_path, proof_file, verus_bin),
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert "verified" in completed.stdout, (
        f"expected Verus proof output; stdout: {completed.stdout}; "
        f"stderr: {completed.stderr}"
    )


@pytest.mark.parametrize(
    "proof_env_var",
    ["VERUS_PROOF_FILE", "INPUT_PROOF_FILE"],
)
def test_verus_run_env_proof_file_is_not_split_on_path_separator(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    proof_env_var: str,
) -> None:
    """A legacy env-var proof path keeps path separators inside one filename."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    # Use the platform's own separator so the directory name stays legal on
    # Windows while still exercising the split Cyclopts would otherwise apply.
    proof_dir = tmp_path / f"proofs{os.pathsep}separator"
    proof_dir.mkdir()
    proof_file = proof_dir / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(proof_command="printf 'verified %s\\n' \"$1\""),
    )

    environment = sanitized_subprocess_env()
    environment[proof_env_var] = str(proof_file)
    environment["VERUS_BIN"] = str(verus_bin)
    environment["INPUT_REPO_ROOT"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--no-ensure-toolchain",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert f"verified {proof_file}" in completed.stdout, (
        f"expected one proof invocation; stdout: {completed.stdout}; "
        f"stderr: {completed.stderr}"
    )


def test_verus_run_streams_every_line_when_the_reader_stays(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """A reader that stays receives the whole output and the prover's status.

    This is the no-broken-pipe reference for the piped cases: streaming must not
    become conditional, and the failing status must survive the ordinary path.
    """
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )

    completed = subprocess.run(
        fake_verus_argv(tmp_path, proof_file, verus_bin),
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=CHILD_TIMEOUT,
    )

    assert completed.returncode == 3, (
        f"expected the prover's status 3, got {completed.returncode}; "
        f"stderr: {completed.stderr}"
    )
    # Compare the whole sequence: a passing check on the last line alone would
    # not notice an earlier line going missing from the stream.
    expected_lines = [f"proof line {index}" for index in range(PROOF_LINES)]
    actual_lines = completed.stdout.splitlines()
    mismatches = [
        (index, wanted, actual)
        for index, (actual, wanted) in enumerate(
            zip(actual_lines, expected_lines, strict=False)
        )
        if actual != wanted
    ][:5]
    assert len(actual_lines) == len(expected_lines), (
        f"expected {len(expected_lines)} streamed lines, got {len(actual_lines)}"
    )
    assert not mismatches, (
        f"expected the streamed lines in order; first mismatches: {mismatches!r}"
    )
