"""Shared helpers for Verus unit tests."""

from __future__ import annotations

import stat
import typing as typ

import rust_prover_tools.verus.install as verus_install_module

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from rust_prover_tools.downloads import PinnedAsset


def make_executable(path: Path, content: str = "#!/bin/sh\n") -> None:
    """Create an executable file for tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def write_verus_pins(
    repo_root: Path,
    version: str = "2025.01.01",
    *,
    target: str = "x86-linux",
) -> None:
    """Write Verus version and checksum pins for `target`."""
    tools_dir = repo_root / "tools" / "verus"
    tools_dir.mkdir(parents=True)
    (tools_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tools_dir / "SHA256SUMS").write_text(
        f"abc123 verus-{version}-{target}.zip\n",
        encoding="utf-8",
    )


def stub_archive_download(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[PinnedAsset, Path]]:
    """Replace the network download and capture each pinned asset request."""
    captured: list[tuple[PinnedAsset, Path]] = []

    def fake_download_pinned(asset: PinnedAsset, destination: Path) -> Path:
        captured.append((asset, destination))
        destination.write_bytes(b"archive")
        return destination

    monkeypatch.setattr(verus_install_module, "download_pinned", fake_download_pinned)
    return captured
