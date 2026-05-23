"""End-to-end tests for externally observable CLI workflows."""

from __future__ import annotations

import os
import subprocess
import sys
import typing as typ

from tests.conftest import FakeVerusBinarySpec

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def sanitized_subprocess_env() -> dict[str, str]:
    """Return subprocess environment without GitHub Actions input bindings."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("INPUT_")
    }


def test_verus_run_cli_uses_existing_binary(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """The public CLI runs an existing Verus binary against a proof file."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(proof_command="printf 'verified %s\\n' \"$1\""),
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--repo-root",
            str(tmp_path),
            "--proof-file",
            str(proof_file),
            "--verus-bin",
            str(verus_bin),
            "--no-ensure-toolchain",
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
    assert "verified" in completed.stdout, (
        f"expected Verus proof output; stdout: {completed.stdout}; "
        f"stderr: {completed.stderr}"
    )
