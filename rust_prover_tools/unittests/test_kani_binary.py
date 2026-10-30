"""Tests for the prebuilt Kani installation route."""

from __future__ import annotations

import io
import os
import tarfile
import typing as typ

import pytest

from rust_prover_tools import kani_binary, platforms
from rust_prover_tools.binary_assets import (
    KANI_BUNDLE_ASSET,
    KANI_FRONTEND_ASSET,
)
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani import KaniInstallOptions, KaniPaths, install_kani
from rust_prover_tools.kani_binary import (
    KaniBinaryInstallOptions,
    default_kani_install_dir,
    install_kani_binary,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cmd_mox import CmdMox, Invocation

    from rust_prover_tools.downloads import PinnedAsset

pytest_plugins = ("cmd_mox.pytest_plugin",)


def write_tar(path: Path, members: dict[str, bytes]) -> Path:
    """Write a gzip-compressed tar archive with executable members."""
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
    return path


def stub_downloads(
    monkeypatch: pytest.MonkeyPatch,
    frontend: Path,
    bundle: Path,
) -> None:
    """Replace verified downloads with pre-built local archives."""
    paths = {
        KANI_FRONTEND_ASSET.name: frontend,
        KANI_BUNDLE_ASSET.name: bundle,
    }

    def fake_download(asset: PinnedAsset, destination: Path) -> Path:
        del destination
        return paths[asset.name]

    monkeypatch.setattr(kani_binary, "download_pinned", fake_download)


def binary_options(
    tmp_path: Path,
    *,
    install_dir: Path,
    cache_dir: Path,
) -> KaniInstallOptions:
    """Return options selecting the prebuilt route with explicit paths."""
    return KaniInstallOptions(
        paths=KaniPaths(repo_root=tmp_path),
        install_dir=install_dir,
        cache_dir=cache_dir,
    )


def test_install_kani_binary_extracts_and_sets_up(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default route installs verified binaries and runs setup."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.67.0\n", encoding="utf-8")
    frontend = write_tar(
        tmp_path / "frontend.tar.gz",
        {"cargo-kani": b"cargo-kani", "kani": b"kani"},
    )
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    install_dir = tmp_path / "bin"
    cache_dir = tmp_path / "cache"
    stub_downloads(monkeypatch, frontend, bundle)

    def cargo_handler(invocation: Invocation) -> tuple[str, str, int]:
        if invocation.args == ["kani", "--version"]:
            return ("kani 0.67.0\n", "", 0)
        return ("", "", 0)

    cmd_mox.stub("cargo").runs(cargo_handler)

    lines = install_kani(
        binary_options(
            tmp_path,
            install_dir=install_dir,
            cache_dir=cache_dir,
        )
    )

    assert lines == (
        f"Installing Kani 0.67.0 prebuilt binaries from {version_file}.",
        f"Extracting Kani 0.67.0 binaries into {install_dir}.",
        "Running cargo kani setup for Kani 0.67.0.",
        "Verifying cargo kani is callable.",
        "kani 0.67.0",
    )
    assert [call.args for call in cmd_mox.journal] == [
        ["kani", "setup", "--use-local-bundle", str(bundle)],
        ["kani", "--version"],
    ], "setup must use the verified local bundle before verification"
    for name in ("cargo-kani", "kani"):
        extracted = install_dir / name
        assert extracted.is_file(), f"{name} should be extracted"
        assert os.access(extracted, os.X_OK), f"{name} should be executable"
        assert extracted.read_bytes() == name.encode()


def test_install_kani_binary_can_skip_setup_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional binary-install steps can be skipped."""
    frontend = write_tar(
        tmp_path / "frontend.tar.gz",
        {"cargo-kani": b"cargo-kani", "kani": b"kani"},
    )
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    stub_downloads(monkeypatch, frontend, bundle)
    install_dir = tmp_path / "bin"

    lines = install_kani_binary(
        KaniBinaryInstallOptions(
            repo_root=tmp_path,
            version="0.67.0",
            version_file=tmp_path / "VERSION",
            install_dir=install_dir,
            cache_dir=tmp_path / "cache",
            should_setup=False,
            should_verify=False,
        ),
    )

    assert lines == (
        f"Installing Kani 0.67.0 prebuilt binaries from {tmp_path / 'VERSION'}.",
        f"Extracting Kani 0.67.0 binaries into {install_dir}.",
    )
    assert (install_dir / "kani").is_file()


def test_install_kani_binary_rejects_an_unpinned_version(
    tmp_path: Path,
) -> None:
    """A version without a pinned prebuilt manifest points at --compile."""
    with pytest.raises(ProverToolError, match=r"--compile"):
        install_kani(
            KaniInstallOptions(
                paths=KaniPaths(repo_root=tmp_path),
                version="0.61.0",
            ),
        )


def test_install_kani_binary_rejects_unsupported_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prebuilt route names the host and the compile fallback."""
    monkeypatch.setattr(platforms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "arm64")

    with pytest.raises(ProverToolError, match="Linux x86_64") as excinfo:
        install_kani_binary(
            KaniBinaryInstallOptions(
                repo_root=tmp_path,
                version="0.67.0",
                version_file=tmp_path / "VERSION",
            ),
        )

    assert "--compile" in str(excinfo.value)


def test_extract_frontend_rejects_a_missing_member(
    tmp_path: Path,
) -> None:
    """An archive missing either executable is rejected."""
    archive = write_tar(tmp_path / "frontend.tar.gz", {"cargo-kani": b"one"})

    with pytest.raises(ProverToolError, match="missing kani"):
        kani_binary._extract_frontend(archive, tmp_path / "bin")


def test_extract_frontend_rejects_an_oversized_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extracted member cannot exceed its configured ceiling."""
    archive = write_tar(
        tmp_path / "frontend.tar.gz",
        {"cargo-kani": b"12345", "kani": b"12345"},
    )
    monkeypatch.setattr(kani_binary, "KANI_FRONTEND_MEMBER_MAX_BYTES", 4)

    with pytest.raises(ProverToolError, match="extraction limit"):
        kani_binary._extract_frontend(archive, tmp_path / "bin")

    assert not (tmp_path / "bin" / "cargo-kani").exists()


def test_path_env_puts_the_install_directory_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setup child sees the extracted binaries before the parent PATH."""
    monkeypatch.setenv("PATH", "/usr/bin")
    install_dir = tmp_path / "bin"

    env = kani_binary._path_env(install_dir)

    assert env["PATH"].split(os.pathsep)[0] == str(install_dir)


def test_default_install_dir_honours_cargo_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CARGO_HOME selects the default binary directory."""
    cargo_home = tmp_path / "cargo-home"
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))

    assert default_kani_install_dir() == cargo_home / "bin"
