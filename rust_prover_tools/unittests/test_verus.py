"""Tests for Verus workflow orchestration."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.unittests.verus_support import (
    make_executable,
    write_verus_pins,
)
from rust_prover_tools.verus import (
    EXAMPLE_VERUS_PROOF_PATH,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    run_verus,
)

if typ.TYPE_CHECKING:
    import pathlib
    from pathlib import Path

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


def test_run_verus_uses_existing_binary_when_version_probe_succeeds(
    tmp_path: Path,
) -> None:
    """Successful Verus version probes do not require rustup."""
    write_verus_pins(tmp_path)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-2025-01-01\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    lines = run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_files=(proof_file,),
        ),
    )

    assert lines == (), "Run should return no output lines when proof succeeds"


def test_run_verus_installs_missing_toolchain(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """A missing Rust toolchain is installed before proof execution."""
    write_verus_pins(tmp_path)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    toolchain_marker = tmp_path / "nightly-missing-installed"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  if [ -f '{toolchain_marker}' ]; then\n"
        "    printf 'Verus\\nToolchain: nightly-missing\\n'\n"
        "    exit 0\n"
        "  fi\n"
        "  printf 'required rust toolchain nightly-missing\\n' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )

    def rustup_handler(invocation: Invocation) -> tuple[str, str, int]:
        if invocation.args == ["which", "--toolchain", "nightly-missing", "rustc"]:
            return ("", "", 1)
        if invocation.args == ["toolchain", "install", "nightly-missing"]:
            toolchain_marker.write_text("installed\n", encoding="utf-8")
            return ("", "", 0)
        return ("", f"unexpected rustup args: {invocation.args}\n", 2)

    cmd_mox.stub("rustup").runs(rustup_handler)

    run_verus(
        VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_files=(proof_file,))
    )

    assert [call.args for call in cmd_mox.journal] == [
        ["which", "--toolchain", "nightly-missing", "rustc"],
        ["toolchain", "install", "nightly-missing"],
    ], "Failed version probe should trigger rustup install"


def test_run_verus_raises_when_proof_fails(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Verifier non-zero exits propagate as proof failures."""
    write_verus_pins(tmp_path)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 5\n",
    )

    with pytest.raises(VerusProofFailedError, match="Verus proofs failed") as exc_info:
        run_verus(
            VerusRunOptions(
                paths=VerusPaths(repo_root=tmp_path), proof_files=(proof_file,)
            ),
        )

    assert exc_info.value.exit_code == 5, "Proof failure should keep the exit code"
    assert f"Proof file: {proof_file}" in str(exc_info.value), (
        "Single-file proof failure should name the proof file"
    )
    assert "Proved before the failure" not in str(exc_info.value), (
        "Single-file failure should not claim an earlier partial result"
    )


def test_run_verus_proves_each_proof_file_in_order(tmp_path: Path) -> None:
    """Repeated proof files each get their own verifier invocation, in order."""
    write_verus_pins(tmp_path)
    proof_files = tuple(tmp_path / name for name in ("first.rs", "second.rs"))
    for proof_file in proof_files:
        proof_file.write_text("proof", encoding="utf-8")
    invocation_log = tmp_path / "invocations.log"
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$*" >> "{invocation_log}"\n',
    )

    run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_files=proof_files,
            extra_args=("--crate-type=lib",),
        ),
    )

    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        f"{proof_file} --crate-type=lib" for proof_file in proof_files
    ], "Each proof file should be verified once, in the order given"


def test_run_verus_stops_at_first_failing_proof_file(
    tmp_path: Path,
) -> None:
    """A later failure names its own file and reports the earlier proved file."""
    write_verus_pins(tmp_path)
    proved_file = tmp_path / "proved.rs"
    failing_file = tmp_path / "failing.rs"
    unrun_file = tmp_path / "unrun.rs"
    for proof_file in (proved_file, failing_file, unrun_file):
        proof_file.write_text("proof", encoding="utf-8")
    invocation_log = tmp_path / "invocations.log"
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$1" >> "{invocation_log}"\n'
        f'if [ "$1" = "{failing_file}" ]; then\n'
        "  exit 7\n"
        "fi\n"
        "exit 0\n",
    )

    with pytest.raises(VerusProofFailedError) as exc_info:
        run_verus(
            VerusRunOptions(
                paths=VerusPaths(repo_root=tmp_path),
                proof_files=(proved_file, failing_file, unrun_file),
            ),
        )

    message = str(exc_info.value)
    assert exc_info.value.exit_code == 7, "Failing file should supply the exit code"
    assert f"Proof file: {failing_file}" in message, (
        "Failure should name the file that failed, not the first file"
    )
    assert f"Proved before the failure: {proved_file}" in message, (
        "Failure should report the file proved before it"
    )
    assert str(unrun_file) not in message, (
        "Files after the failure should not be reported as proved or failed"
    )
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        str(proved_file),
        str(failing_file),
    ], "Run should stop at the first failing proof file"


def test_run_verus_rejects_missing_proof_file_before_proving(tmp_path: Path) -> None:
    """Every proof file is checked before the first verifier invocation."""
    write_verus_pins(tmp_path)
    present_file = tmp_path / "present.rs"
    present_file.write_text("proof", encoding="utf-8")
    missing_file = tmp_path / "missing.rs"
    invocation_log = tmp_path / "invocations.log"
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$1" >> "{invocation_log}"\n'
        "exit 0\n",
    )

    with pytest.raises(ProverToolError, match="Verus proof file not found") as exc_info:
        run_verus(
            VerusRunOptions(
                paths=VerusPaths(repo_root=tmp_path),
                proof_files=(present_file, missing_file),
            ),
        )

    assert str(missing_file) in str(exc_info.value), (
        "Missing-file error should name the path that does not exist"
    )
    assert not invocation_log.exists(), (
        "The preflight check must reject the batch before invoking the verifier"
    )


def test_run_verus_verifies_compatibility_default_when_none_requested(
    tmp_path: Path,
) -> None:
    """An empty proof-file request still verifies the compatibility default."""
    write_verus_pins(tmp_path)
    default_proof = tmp_path / EXAMPLE_VERUS_PROOF_PATH
    default_proof.parent.mkdir(parents=True, exist_ok=True)
    default_proof.write_text("proof", encoding="utf-8")
    invocation_log = tmp_path / "invocations.log"
    binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$1" >> "{invocation_log}"\n'
        "exit 0\n",
    )

    run_verus(VerusRunOptions(paths=VerusPaths(repo_root=tmp_path)))

    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        str(default_proof)
    ], "No proof file should fall back to the compatibility example path"


@pytest.mark.parametrize("use_positional", [False, True], ids=["keyword", "positional"])
def test_verus_run_options_accepts_legacy_single_proof_file(
    tmp_path: Path,
    *,
    use_positional: bool,
) -> None:
    """The former single-path construction keeps working."""
    path = tmp_path / "proof.rs"
    paths = VerusPaths(repo_root=tmp_path)
    options = (
        VerusRunOptions(paths, path)
        if use_positional
        else VerusRunOptions(paths=paths, proof_file=path)
    )

    assert options.proof_files == (path,), (
        "Legacy single-path construction should fold into proof_files"
    )


def test_verus_run_options_rejects_both_proof_file_forms(tmp_path: Path) -> None:
    """The deprecated alias cannot silently override the tuple field."""
    path = tmp_path / "proof.rs"

    with pytest.raises(TypeError, match="not both"):
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_file=path,
            proof_files=(path,),
        )


@pytest.mark.parametrize("use_positional", [False, True], ids=["keyword", "positional"])
def test_verus_run_options_rejects_bare_path_as_proof_files(
    tmp_path: Path,
    *,
    use_positional: bool,
) -> None:
    """A bare Path in the tuple slot cannot be mistaken for one proof file."""
    path = tmp_path / "proof.rs"
    bare_path = typ.cast("tuple[pathlib.Path, ...]", path)

    def build() -> None:
        if use_positional:
            # The former third positional slot was `install_dir`.
            VerusRunOptions(VerusPaths(repo_root=tmp_path), None, bare_path)
        else:
            VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_files=bare_path)

    with pytest.raises(TypeError, match=r"tuple of pathlib\.Path values"):
        build()


def test_run_verus_rejects_missing_proof_file(tmp_path: Path) -> None:
    """Missing proof files fail before resolving binaries."""
    write_verus_pins(tmp_path)
    missing_proof = tmp_path / "missing.rs"

    with pytest.raises(ProverToolError, match="Verus proof file not found"):
        run_verus(
            VerusRunOptions(
                paths=VerusPaths(repo_root=tmp_path),
                proof_files=(missing_proof,),
            ),
        )
