"""Tests for Verus installation and archive pinning."""

from __future__ import annotations

import logging
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
    import os
    from pathlib import Path

    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)


def events_from(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured events captured during a test."""
    return [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]


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


def test_install_verus_reports_extraction_and_completion_events(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful install records extraction and completion boundaries."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.verus.install")
    write_verus_pins(tmp_path)
    install_dir = tmp_path / ".verus" / "2025.01.01"
    make_executable(install_dir / "verus-x86-linux" / "verus")
    stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns()

    install_verus(
        VerusInstallOptions(
            paths=VerusPaths(repo_root=tmp_path),
            install_dir=install_dir,
            base_url="https://example.test",
        ),
    )

    events = events_from(caplog)
    asset_name = "verus-2025.01.01-x86-linux.zip"
    assert [event["event"] for event in events] == [
        "verus.install.start",
        "verus.extract.start",
        "verus.extract.complete",
        "verus.install.complete",
    ], "each install boundary should record one event in order"
    assert events[0]["version"] == "2025.01.01", "start reports the version"
    assert events[0]["target"] == "x86-linux", "start reports the target"
    assert events[1]["asset"] == asset_name, "extraction start names the asset"
    assert events[2]["asset"] == asset_name, "extraction completion names the asset"
    complete = events[3]
    assert complete["version"] == "2025.01.01", "completion reports the version"
    elapsed = complete["elapsed_seconds"]
    assert isinstance(elapsed, float), "completion must carry a float duration"
    assert elapsed >= 0.0, "completion must carry a non-negative duration"


def test_install_verus_reports_reuse_instead_of_a_reinstall(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An installed binary reports reuse and stops before downloading."""
    write_verus_pins(tmp_path)
    install_dir = tmp_path / ".verus" / "2025.01.01"
    make_executable(install_dir / "verus-x86-linux" / "verus")
    stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns()
    options = VerusInstallOptions(
        paths=VerusPaths(repo_root=tmp_path),
        install_dir=install_dir,
        base_url="https://example.test",
    )
    install_verus(options)
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.verus.install")

    install_verus(options)

    assert events_from(caplog) == [
        {
            "event": "verus.install.start",
            "version": "2025.01.01",
            "target": "x86-linux",
        },
        {"event": "verus.install.reuse", "version": "2025.01.01"},
    ], "reuse should be visible without extraction or completion events"


def test_install_verus_reports_a_download_failure_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed archive download reports one bounded reason."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.verus.install")
    write_verus_pins(tmp_path)

    def fail_download(asset: PinnedAsset, destination: Path) -> Path:
        del asset, destination
        msg = "digest mismatch"
        raise ProverToolError(msg)

    monkeypatch.setattr(verus_install_module, "download_pinned", fail_download)

    with pytest.raises(ProverToolError, match="digest mismatch"):
        install_verus(
            VerusInstallOptions(
                paths=VerusPaths(repo_root=tmp_path),
                install_dir=tmp_path / ".verus" / "2025.01.01",
                base_url="https://example.test",
            ),
        )

    assert events_from(caplog) == [
        {
            "event": "verus.install.start",
            "version": "2025.01.01",
            "target": "x86-linux",
        },
        {"event": "verus.install.failed", "reason": "download"},
    ], "a download failure must not be reported as an extraction failure"


def test_install_verus_reports_an_extract_failure_event(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing unzip command reports the extraction boundary."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.verus.install")
    write_verus_pins(tmp_path)
    stub_archive_download(monkeypatch)
    cmd_mox.stub("unzip").returns(stderr="bad archive\n", exit_code=2)

    with pytest.raises(ProverToolError, match="bad archive"):
        install_verus(
            VerusInstallOptions(
                paths=VerusPaths(repo_root=tmp_path),
                install_dir=tmp_path / ".verus" / "2025.01.01",
                base_url="https://example.test",
            ),
        )

    events = events_from(caplog)
    assert [event["event"] for event in events] == [
        "verus.install.start",
        "verus.extract.start",
        "verus.install.failed",
    ], "a failed extraction should record its start and one failure reason"
    assert events[-1]["reason"] == "extract", "the failure reason must be extract"


def test_install_verus_reports_an_inspection_failure_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable installation is reported instead of treated as absent."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.verus.install")
    write_verus_pins(tmp_path)
    install_dir = tmp_path / ".verus" / "2025.01.01"
    installed_bin = install_dir / "verus" / "verus"
    original_stat = pathlib.Path.stat

    def deny_stat(
        path: pathlib.Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == installed_bin:
            raise PermissionError(13, "permission denied", str(path))
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(pathlib.Path, "stat", deny_stat)

    with pytest.raises(ProverToolError, match="failed to inspect Verus installation"):
        install_verus(
            VerusInstallOptions(
                paths=VerusPaths(repo_root=tmp_path),
                install_dir=install_dir,
                base_url="https://example.test",
            ),
        )

    assert events_from(caplog) == [
        {
            "event": "verus.install.start",
            "version": "2025.01.01",
            "target": "x86-linux",
        },
        {"event": "verus.install.failed", "reason": "inspect"},
    ], "an inspection failure must report one bounded reason"
