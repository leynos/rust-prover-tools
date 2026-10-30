"""Tests for the pinned Z3 solver installer."""

from __future__ import annotations

import os
import typing as typ
import zipfile

import pytest

import rust_prover_tools.z3 as z3_module
from rust_prover_tools import platforms
from rust_prover_tools.binary_assets import Z3_ASSET, Z3_MEMBER, Z3_VERSION
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.z3 import (
    Z3InstallOptions,
    default_z3_cache_dir,
    default_z3_install_dir,
    install_z3,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.downloads import PinnedAsset


def write_z3_zip(path: Path, *, payload: bytes = b"z3-binary") -> Path:
    """Write a minimal Z3-shaped zip archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(Z3_MEMBER, payload)
    return path


def stub_download(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,
    *,
    expected_destination: Path,
) -> None:
    """Replace the verified download with a local archive."""

    def fake_download(asset: PinnedAsset, destination: Path) -> Path:
        assert asset is Z3_ASSET, "the pinned Z3 asset must be requested"
        assert destination == expected_destination, "the cache path must be used"
        return archive

    monkeypatch.setattr(z3_module, "download_pinned", fake_download)


def test_install_z3_extracts_only_the_pinned_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installer writes an executable z3 and its version marker."""
    install_dir = tmp_path / "z3-install"
    cache_dir = tmp_path / "cache"
    archive = write_z3_zip(cache_dir / Z3_ASSET.name)
    stub_download(
        monkeypatch,
        archive,
        expected_destination=cache_dir / Z3_ASSET.name,
    )

    lines = install_z3(
        Z3InstallOptions(
            repo_root=tmp_path,
            install_dir=install_dir,
            cache_dir=cache_dir,
        ),
    )

    binary = install_dir / "z3"
    assert lines == (
        f"Installed Z3 {Z3_VERSION} at {binary}",
        f"Export VERUS_Z3_PATH={binary}",
    )
    assert binary.read_bytes() == b"z3-binary"
    assert os.access(binary, os.X_OK), "z3 must be executable"
    assert (install_dir / ".z3-version").read_text(encoding="utf-8").strip() == (
        Z3_VERSION
    )


def test_install_z3_reuses_a_marked_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching marker avoids another download and extraction."""
    install_dir = tmp_path / "z3-install"
    cache_dir = tmp_path / "cache"
    archive = write_z3_zip(cache_dir / Z3_ASSET.name)
    stub_download(
        monkeypatch,
        archive,
        expected_destination=cache_dir / Z3_ASSET.name,
    )
    options = Z3InstallOptions(
        repo_root=tmp_path,
        install_dir=install_dir,
        cache_dir=cache_dir,
    )
    install_z3(options)

    def fail_download(asset: PinnedAsset, destination: Path) -> Path:
        del asset, destination
        msg = "a marked installation must not be downloaded again"
        raise AssertionError(msg)

    monkeypatch.setattr(z3_module, "download_pinned", fail_download)

    lines = install_z3(options)

    binary = install_dir / "z3"
    assert lines == (
        f"Z3 {Z3_VERSION} already installed at {binary}",
        f"Export VERUS_Z3_PATH={binary}",
    )


def test_install_z3_rejects_a_missing_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archive without the pinned binary is rejected."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    archive = cache_dir / Z3_ASSET.name
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("other/bin/z3", b"wrong")
    monkeypatch.setattr(
        z3_module,
        "download_pinned",
        lambda asset, destination: archive,
    )

    with pytest.raises(ProverToolError, match="missing"):
        install_z3(
            Z3InstallOptions(
                repo_root=tmp_path,
                install_dir=tmp_path / "install",
                cache_dir=cache_dir,
            ),
        )

    assert not (tmp_path / "install" / "z3").exists()


def test_install_z3_rejects_an_oversized_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extracted binary cannot exceed its configured ceiling."""
    cache_dir = tmp_path / "cache"
    archive = write_z3_zip(cache_dir / Z3_ASSET.name, payload=b"12345")
    monkeypatch.setattr(
        z3_module,
        "download_pinned",
        lambda asset, destination: archive,
    )
    monkeypatch.setattr(z3_module, "Z3_EXTRACTED_MAX_BYTES", 4)

    with pytest.raises(ProverToolError, match="extraction limit"):
        install_z3(
            Z3InstallOptions(
                repo_root=tmp_path,
                install_dir=tmp_path / "install",
                cache_dir=cache_dir,
            ),
        )

    assert not (tmp_path / "install" / "z3").exists()


def test_install_z3_rejects_unsupported_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned Z3 archive is only offered on Linux x86_64."""
    monkeypatch.setattr(platforms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "arm64")

    with pytest.raises(ProverToolError, match="Linux x86_64"):
        install_z3(Z3InstallOptions(repo_root=tmp_path))


def test_default_z3_paths_are_repository_local(tmp_path: Path) -> None:
    """Default cache and install directories stay below the repository root."""
    assert default_z3_cache_dir(tmp_path) == tmp_path / ".cache" / "z3"
    assert default_z3_install_dir(tmp_path) == (tmp_path / ".cache" / "z3" / Z3_VERSION)
