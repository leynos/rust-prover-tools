"""Tests for Verus installation and archive pinning."""

from __future__ import annotations

import pathlib
import typing as typ

import pytest

import rust_prover_tools.verus.install as verus_install_module
from rust_prover_tools.downloads import PinnedAsset
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.unittests import conftest
from rust_prover_tools.unittests.verus_support import (
    make_executable,
    stub_archive_download,
    write_verus_pins,
)
from rust_prover_tools.verus import (
    VerusInstallOptions,
    VerusPaths,
    VerusRunOptions,
    install_verus,
    resolve_verus_bin,
    run_verus,
)
from rust_prover_tools.verus.install import VERUS_ARCHIVE_MAX_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)


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


def test_install_verus_requests_pinned_archive_and_normalizes_it(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verus installation uses the shared pinned downloader, then unzips."""
    write_verus_pins(tmp_path)
    install_dir = tmp_path / ".verus" / "2025.01.01"
    extracted_binary = install_dir / "verus-x86-linux" / "verus"
    make_executable(extracted_binary)
    downloads = stub_archive_download(monkeypatch)
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
        "unzip",
    ], "The installer should use the Python downloader and external unzip only"
    assert len(downloads) == 1, "Install should request exactly one archive"
    asset, archive_path = downloads[0]
    assert asset == PinnedAsset(
        name="verus-2025.01.01-x86-linux.zip",
        url="https://example.test/2025.01.01/verus-2025.01.01-x86-linux.zip",
        sha256="abc123",
        max_bytes=VERUS_ARCHIVE_MAX_BYTES,
    ), "The download should carry the pinned name, URL, digest, and ceiling"
    assert archive_path.name == "verus-release.zip", (
        "The local archive name must not come from repository input"
    )
    assert cmd_mox.journal[0].args == [
        "-q",
        str(archive_path),
        "-d",
        str(install_dir),
    ], "unzip should extract the downloaded archive to install_dir"
    assert (install_dir / "verus" / "verus").exists(), (
        "Install should normalize final binary path"
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
    stub_archive_download(monkeypatch)
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
        "unzip",
    ], "Relative pin overrides should still extract the downloaded archive"


@pytest.mark.parametrize(
    ("field", "match"),
    [
        ("version", "invalid Verus version"),
        ("target", "invalid Verus target"),
    ],
)
@pytest.mark.parametrize(
    "unsafe_component",
    ["", "..", "../escape", "-rf", "a/b", "a\\b", "a\nb", "/absolute"],
)
def test_verus_archive_rejects_unsafe_components(
    tmp_path: Path,
    field: str,
    match: str,
    unsafe_component: str,
) -> None:
    """A version and target must each be one safe path and URL component."""
    options = VerusInstallOptions(paths=VerusPaths(repo_root=tmp_path))
    version = "2025.01.01"
    if field == "target":
        options = VerusInstallOptions(
            paths=VerusPaths(repo_root=tmp_path),
            target=unsafe_component,
        )
    else:
        version = unsafe_component

    with pytest.raises(ProverToolError, match=match):
        verus_install_module.verus_archive(options, version)


def test_install_verus_rejects_traversal_version_before_creating_paths(
    tmp_path: Path,
) -> None:
    """A traversal in the version pin must fail before its path is used."""
    tools_dir = tmp_path / "tools" / "verus"
    tools_dir.mkdir(parents=True)
    (tools_dir / "VERSION").write_text("../escape\n", encoding="utf-8")

    with pytest.raises(ProverToolError, match="invalid Verus version"):
        install_verus(VerusInstallOptions(paths=VerusPaths(repo_root=tmp_path)))

    assert not (tmp_path / "escape").exists(), (
        "Rejected traversal must not create a directory outside .verus"
    )


def test_install_verus_lowercases_the_pinned_digest(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checksum rows are accepted in either hex case."""
    tools_dir = tmp_path / "tools" / "verus"
    tools_dir.mkdir(parents=True)
    (tools_dir / "VERSION").write_text("2025.01.01\n", encoding="utf-8")
    digest = "ABCDEF0123456789" * 4
    (tools_dir / "SHA256SUMS").write_text(
        f"{digest} verus-2025.01.01-x86-linux.zip\n",
        encoding="utf-8",
    )
    install_dir = tmp_path / ".verus" / "2025.01.01"
    make_executable(install_dir / "verus-x86-linux" / "verus")
    downloads = stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns()

    install_verus(
        VerusInstallOptions(
            paths=VerusPaths(repo_root=tmp_path),
            install_dir=install_dir,
            base_url="https://example.test",
        ),
    )

    assert downloads[0][0].sha256 == digest.lower(), (
        "The downloader compares against lowercase hexadecimal digests"
    )


def test_run_verus_installs_missing_default_binary(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
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
    stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns()

    run_verus(
        VerusRunOptions(paths=VerusPaths(repo_root=tmp_path), proof_files=(proof_file,))
    )

    assert cmd_mox.journal[0].command == "unzip", (
        "Missing default binary should trigger the installer's unzip step"
    )


def test_run_verus_forwards_target_to_missing_binary_installer(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-install fallback fetches the requested Verus target archive."""
    target = "aarch64-macos"
    write_verus_pins(tmp_path, target=target)
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
    downloads = stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns()

    run_verus(
        VerusRunOptions(
            paths=VerusPaths(repo_root=tmp_path),
            proof_files=(proof_file,),
            target=target,
        ),
    )

    assert downloads[0][0].url.endswith("verus-2025.01.01-aarch64-macos.zip"), (
        "Fallback installer should fetch the requested Verus target"
    )


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
