"""Tests for Verus workflow orchestration."""

from __future__ import annotations

import stat
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.verus import (
    VerusInstallOptions,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    install_verus,
    resolve_verus_bin,
    run_verus,
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


def test_run_verus_uses_existing_binary_and_installed_toolchain(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Verus proof runs preserve version, rustup, and proof command order."""
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
    cmd_mox.mock("rustup").with_args(
        "which",
        "--toolchain",
        "nightly-2025-01-01",
        "rustc",
    ).returns()
    lines = run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_file=proof_file,
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
    make_executable(
        binary,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'Verus\\nToolchain: nightly-missing\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    def rustup_handler(invocation: Invocation) -> tuple[str, str, int]:
        if invocation.args == ["which", "--toolchain", "nightly-missing", "rustc"]:
            return ("", "", 1)
        if invocation.args == ["toolchain", "install", "nightly-missing"]:
            return ("", "", 0)
        return ("", f"unexpected rustup args: {invocation.args}\n", 2)

    cmd_mox.stub("rustup").runs(rustup_handler)

    run_verus(
        VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_file=proof_file)
    )

    assert [call.args for call in cmd_mox.journal] == [
        ["which", "--toolchain", "nightly-missing", "rustc"],
        ["toolchain", "install", "nightly-missing"],
    ], "Missing toolchain should trigger rustup install"


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
    cmd_mox.mock("rustup").with_args(
        "which",
        "--toolchain",
        "nightly-test",
        "rustc",
    ).returns()

    with pytest.raises(VerusProofFailedError, match="Verus proofs failed"):
        run_verus(
            VerusRunOptions(
                paths=VerusPaths(repo_root=tmp_path), proof_file=proof_file
            ),
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
    cmd_mox.mock("rustup").with_args(
        "which",
        "--toolchain",
        "nightly-installed",
        "rustc",
    ).returns()

    run_verus(
        VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_file=proof_file)
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
    cmd_mox.mock("rustup").with_args(
        "which",
        "--toolchain",
        "nightly-target",
        "rustc",
    ).returns()

    run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_file=proof_file,
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
    cmd_mox.mock("rustup").with_args(
        "which",
        "--toolchain",
        "nightly-test",
        "rustc",
    ).returns()

    lines = run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_file=proof_file,
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
                proof_file=missing_proof,
            ),
        )
