"""Tests for Verus workflow orchestration."""

from __future__ import annotations

import logging
import pathlib
import stat
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import rust_prover_tools.verus.batch as verus_batch_module
from rust_prover_tools.commands import CommandResult, CommandSpec
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.unittests import conftest
from rust_prover_tools.verus import (
    EXAMPLE_VERUS_PROOF_PATH,
    ProofFailureContext,
    VerusInstallOptions,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    VerusRunSettings,
    install_verus,
    resolve_verus_bin,
    run_verus,
    verify_proof_files,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


def make_executable(path: Path, content: str = "#!/bin/sh\n") -> None:
    """Create an executable file for tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def write_verus_pins(repo_root: Path, version: str = "2025.01.01") -> None:
    """Write Verus version and checksum pins."""
    tools_dir = repo_root / "tools" / "verus"
    tools_dir.mkdir(parents=True)
    (tools_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tools_dir / "SHA256SUMS").write_text(
        f"abc123 verus-{version}-x86-linux.zip\n",
        encoding="utf-8",
    )


def write_verus_pins_for_target(
    repo_root: Path,
    *,
    target: str,
    version: str = "2025.01.01",
) -> None:
    """Write Verus version and checksum pins for a specific target."""
    tools_dir = repo_root / "tools" / "verus"
    tools_dir.mkdir(parents=True)
    (tools_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tools_dir / "SHA256SUMS").write_text(
        f"abc123 verus-{version}-{target}.zip\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "layout",
    [
        "verus/verus",
        "verus",
        "bin/verus",
    ],
)
def test_resolve_verus_bin_accepts_supported_directory_layouts(
    tmp_path: Path,
    layout: str,
) -> None:
    """Directory resolution checks the layouts supported by the shell script."""
    binary = tmp_path / layout
    make_executable(binary)

    assert resolve_verus_bin(str(tmp_path)) == binary, (
        f"Should resolve binary from {layout} directory layout"
    )


def test_install_verus_downloads_checks_and_normalises_archive(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Verus installation preserves download, checksum, and unzip behaviour."""
    write_verus_pins(tmp_path)
    install_dir = tmp_path / ".verus" / "2025.01.01"
    extracted_binary = install_dir / "verus-x86-linux" / "verus"
    make_executable(extracted_binary)
    cmd_mox.stub("curl").returns()
    cmd_mox.stub("sha256sum").returns(stdout="abc123 archive.zip\n")
    cmd_mox.stub("unzip").returns()

    lines = install_verus(
        VerusInstallOptions(
            paths=VerusPaths(repo_root=tmp_path),
            install_dir=install_dir,
            base_url="https://example.test",
        ),
    )

    assert lines == (
        f"Installed Verus 2025.01.01 in {install_dir / 'verus'}",
        f"Export VERUS_BIN={install_dir / 'verus' / 'verus'}",
    ), "Install should return status and export instruction"
    assert [call.command for call in cmd_mox.journal] == [
        "curl",
        "sha256sum",
        "unzip",
    ], "Should execute curl, sha256sum, and unzip in sequence"
    assert cmd_mox.journal[0].args[:7] == [
        "-sSfL",
        "--connect-timeout",
        "15",
        "--max-time",
        "300",
        "https://example.test/2025.01.01/verus-2025.01.01-x86-linux.zip",
        "-o",
    ], "curl should download from correct URL with correct flags"
    assert (
        cmd_mox
        .journal[1]
        .args[0]
        .endswith(
            "verus-2025.01.01-x86-linux.zip",
        )
    ), "sha256sum should verify the downloaded archive"
    assert cmd_mox.journal[2].args == [
        "-q",
        cmd_mox.journal[0].args[7],
        "-d",
        str(install_dir),
    ], "unzip should extract to install_dir"
    assert (install_dir / "verus" / "verus").exists(), (
        "Install should normalise final binary path"
    )


@conftest.SKIP_CHDIR_UNDER_MUTMUT
def test_install_verus_resolves_relative_pin_files_from_repo_root(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative Verus pin overrides resolve below the configured repository."""
    repo_root = tmp_path / "repo-root"
    pins_dir = repo_root / "pins"
    pins_dir.mkdir(parents=True)
    (pins_dir / "verus-version").write_text("2025.01.01\n", encoding="utf-8")
    (pins_dir / "verus-checksum").write_text(
        "abc123 verus-2025.01.01-x86-linux.zip\n",
        encoding="utf-8",
    )
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    install_dir = repo_root / ".verus" / "2025.01.01"
    extracted_binary = install_dir / "verus-x86-linux" / "verus"
    make_executable(extracted_binary)
    cmd_mox.stub("curl").returns()
    cmd_mox.stub("sha256sum").returns(stdout="abc123 archive.zip\n")
    cmd_mox.stub("unzip").returns()

    lines = install_verus(
        VerusInstallOptions(
            paths=VerusPaths(
                repo_root=repo_root,
                version_file=pathlib.Path("pins/verus-version"),
                checksum_file=pathlib.Path("pins/verus-checksum"),
            ),
            install_dir=install_dir,
            base_url="https://example.test",
        ),
    )

    assert lines == (
        f"Installed Verus 2025.01.01 in {install_dir / 'verus'}",
        f"Export VERUS_BIN={install_dir / 'verus' / 'verus'}",
    ), "Install should read relative pins from repo_root"
    assert [call.command for call in cmd_mox.journal] == [
        "curl",
        "sha256sum",
        "unzip",
    ], "Relative pin overrides should still perform install commands"


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


def test_verify_proof_files_keeps_partial_result_when_invocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early invocation failure still names the files already proved."""
    proved_file = tmp_path / "proved.rs"
    unstartable_file = tmp_path / "unstartable.rs"

    def fake_run_command(spec: CommandSpec) -> CommandResult:
        if spec.argv[1] == str(proved_file):
            return CommandResult(stdout="", stderr="", exit_code=0)
        msg = "verifier disappeared"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with pytest.raises(ProverToolError, match="Verus invocation failed") as exc_info:
        verify_proof_files(
            (proved_file, unstartable_file),
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    message = str(exc_info.value)
    assert f"Proof file: {unstartable_file}" in message, (
        "Invocation failure should name the file that could not run"
    )
    assert f"Proved before the failure: {proved_file}" in message, (
        "Invocation failure should keep the files proved before it"
    )


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


def test_failure_message_quotes_unsafe_paths(tmp_path: Path) -> None:
    """Unsafe path text cannot spoof or split the failure report."""
    unsafe_file = tmp_path / "bad, name\nspoof.rs"
    context = ProofFailureContext(
        7,
        pathlib.Path("/bin/verus"),
        unsafe_file,
        "toolchain",
        (tmp_path / "first.rs",),
    )

    message = str(VerusProofFailedError(context))

    assert repr(str(unsafe_file)) in message, (
        "A path with a newline or list delimiter should be quoted"
    )
    assert "\nspoof.rs" not in message, (
        "A newline in a path must not inject a new diagnostic line"
    )


def test_verify_proof_files_emits_bounded_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Proof-batch telemetry carries counters and classifications, not paths."""
    proof_files = (pathlib.Path("first.rs"), pathlib.Path("second.rs"))

    def fake_run_command(_spec: object) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"):
        verify_proof_files(
            proof_files,
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    events = [
        record
        for record in caplog.records
        if record.name == "rust_prover_tools.verus.batch"
    ]
    assert [getattr(event, "operation", None) for event in events] == [
        "verus-proof-batch-start",
        "verus-proof-invocation-start",
        "verus-proof-invocation",
        "verus-proof-invocation-start",
        "verus-proof-invocation",
        "verus-proof-batch",
    ], "The batch and each invocation should emit paired start and completion events"
    assert getattr(events[-1], "proof_count", None) == 2, (
        "The batch event should report both requested proof files"
    )
    assert getattr(events[-1], "proved_count", None) == 2, (
        "The batch event should report both proof files as proved"
    )
    for event in events:
        payload = str(vars(event))
        assert "first.rs" not in payload, "Telemetry must not carry proof paths"
        assert "second.rs" not in payload, "Telemetry must not carry proof paths"


def test_verify_proof_files_uses_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry durations come from the injected clock, not wall time."""
    proof_files = (pathlib.Path("first.rs"), pathlib.Path("second.rs"))
    ticks = iter([0.0, 1.0, 2.0, 3.0, 5.0, 6.0])

    def fake_run_command(_spec: object) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"):
        verify_proof_files(
            proof_files,
            VerusRunSettings(
                pathlib.Path("/bin/verus"),
                "toolchain",
                clock=lambda: next(ticks),
            ),
        )

    durations = [
        getattr(record, "duration_seconds", None)
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-invocation"
    ]
    assert durations == pytest.approx([1.0, 2.0]), (
        "Each invocation duration should be measured with the injected clock"
    )
    batch_events = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-batch"
    ]
    assert getattr(batch_events[-1], "duration_seconds", None) == pytest.approx(6.0), (
        "The batch duration should span the injected clock's readings"
    )


def test_verify_proof_files_reports_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception outside the handled paths still reports a failed batch."""
    proof_files = (pathlib.Path("first.rs"),)

    def fake_run_command(_spec: object) -> CommandResult:
        msg = "verifier wrapper crashed"
        raise RuntimeError(msg)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with (
        caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"),
        pytest.raises(RuntimeError, match="verifier wrapper crashed"),
    ):
        verify_proof_files(
            proof_files,
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    batch_events = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-batch"
    ]
    assert getattr(batch_events[-1], "status_class", None) == "failure", (
        "An unhandled exception must not be reported as a successful batch"
    )
    assert getattr(batch_events[-1], "error_category", None) == "unexpected", (
        "An unhandled exception should carry the unexpected error category"
    )


def test_run_verus_installs_missing_default_binary(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """A missing default Verus binary triggers installer fallback."""
    write_verus_pins(tmp_path)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    install_dir = tmp_path / ".verus" / "2025.01.01"
    make_executable(
        install_dir / "verus-x86-linux" / "verus",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-installed\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    cmd_mox.stub("curl").returns()
    cmd_mox.stub("sha256sum").returns(stdout="abc123 archive.zip\n")
    cmd_mox.stub("unzip").returns()

    run_verus(
        VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_files=(proof_file,))
    )

    assert [call.command for call in list(cmd_mox.journal)[:3]] == [
        "curl",
        "sha256sum",
        "unzip",
    ], "Missing default binary should trigger installer commands"


def test_run_verus_forwards_target_to_missing_binary_installer(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Auto-install fallback fetches the requested Verus target archive."""
    target = "aarch64-macos"
    write_verus_pins_for_target(tmp_path, target=target)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    install_dir = tmp_path / ".verus" / "2025.01.01"
    make_executable(
        install_dir / f"verus-{target}" / "verus",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-target\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    cmd_mox.stub("curl").returns()
    cmd_mox.stub("sha256sum").returns(stdout="abc123 archive.zip\n")
    cmd_mox.stub("unzip").returns()

    run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_files=(proof_file,),
            target=target,
        ),
    )

    assert (
        cmd_mox
        .journal[0]
        .args[5]
        .endswith(
            "verus-2025.01.01-aarch64-macos.zip",
        )
    ), "Fallback installer should fetch the requested Verus target"


def test_run_verus_warns_and_falls_back_from_invalid_override(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Invalid explicit binaries produce fallback warning lines."""
    write_verus_pins(tmp_path)
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    default_binary = tmp_path / ".verus" / "2025.01.01" / "verus" / "verus"
    make_executable(
        default_binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-test\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    invalid_binary = tmp_path / "missing-verus"

    lines = run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_files=(proof_file,),
            verus_bin=str(invalid_binary),
        ),
    )

    assert lines == (
        f"VERUS_BIN is not executable: {invalid_binary}",
        f"Falling back to {default_binary}",
    ), "Invalid override should return fallback warning lines"


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


@given(
    names=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    failure_position=st.integers(min_value=-1, max_value=4),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_verify_proof_files_runs_an_ordered_prefix(
    names: list[str],
    failure_position: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For any sequence and failure point, only the ordered prefix runs."""
    proof_files = tuple(pathlib.Path(f"{name}.rs") for name in names)
    failing_index = (
        None if failure_position == -1 else failure_position % len(proof_files)
    )
    invocations: list[tuple[str, ...]] = []

    def fake_run_command(spec: CommandSpec) -> CommandResult:
        invocations.append(spec.argv)
        proof_file = pathlib.Path(spec.argv[1])
        exit_code = (
            9
            if failing_index is not None and proof_file == proof_files[failing_index]
            else 0
        )
        return CommandResult(stdout="", stderr="", exit_code=exit_code)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    if failing_index is None:
        verify_proof_files(
            proof_files,
            VerusRunSettings(
                pathlib.Path("/bin/verus"),
                "toolchain",
                ("--extra",),
            ),
        )
        assert [argv[1] for argv in invocations] == [
            str(path) for path in proof_files
        ], "A successful batch should invoke every file in order"
        return

    with pytest.raises(VerusProofFailedError) as exc_info:
        verify_proof_files(
            proof_files,
            VerusRunSettings(
                pathlib.Path("/bin/verus"),
                "toolchain",
                ("--extra",),
            ),
        )

    expected_prefix = proof_files[: failing_index + 1]
    assert [argv[1] for argv in invocations] == [
        str(path) for path in expected_prefix
    ], "Invocation should stop at the first failing file"
    assert all(argv[2:] == ("--extra",) for argv in invocations), (
        "Extra arguments should accompany every invocation"
    )
    assert exc_info.value.exit_code == 9, "The failing invocation should set the code"
    message = str(exc_info.value)
    assert f"Proof file: {proof_files[failing_index]}" in message, (
        "The failure should name the file that failed"
    )
    earlier = proof_files[:failing_index]
    if earlier:
        listed = ", ".join(str(path) for path in earlier)
        assert f"Proved before the failure: {listed}" in message, (
            "The failure should report exactly the earlier proved files"
        )
    else:
        assert "Proved before the failure" not in message, (
            "A first-file failure has no earlier proved files to report"
        )
