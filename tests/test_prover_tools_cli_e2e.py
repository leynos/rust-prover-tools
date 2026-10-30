"""End-to-end tests for externally observable CLI workflows."""

from __future__ import annotations

import os
import subprocess
import sys
import typing as typ

import pytest

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
    assert f"proof line {PROOF_LINES - 1}\n" in completed.stdout, (
        "expected every streamed line, including the last; "
        f"stdout ended with: {completed.stdout[-200:]!r}"
    )
# weave: run 'weave explain tests/test_prover_tools_cli_e2e.py' for per-hunk detail, 'weave check' to verify your resolution
