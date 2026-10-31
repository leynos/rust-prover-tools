"""Pinned Z3 solver installation for Verus.

Verus downloads Z3 separately from its own release archive. This module
downloads the pinned official Z3 zip through the bounded verifier in
`rust_prover_tools.downloads`, extracts only `<release>/bin/z3`, and records
the installed version so repeated CI runs can reuse the binary.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ
import zipfile

from rust_prover_tools.binary_assets import (
    COPY_CHUNK_SIZE,
    Z3_ASSET,
    Z3_EXTRACTED_MAX_BYTES,
    Z3_MEMBER,
    Z3_VERSION,
)
from rust_prover_tools.downloads import download_pinned
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.path_resolution import resolve_configured_path
from rust_prover_tools.platforms import require_linux_x86_64

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "Z3InstallOptions",
    "default_z3_cache_dir",
    "default_z3_install_dir",
    "install_z3",
)

MARKER_NAME = ".z3-version"


@dc.dataclass(frozen=True, slots=True)
class Z3InstallOptions:
    """Options for installing the pinned Z3 solver.

    Attributes
    ----------
    repo_root : Path
        Repository root used to resolve relative cache and install paths.
    install_dir : Path | None
        Directory for the `z3` binary. Defaults to `.cache/z3/<version>`
        below the repository root.
    cache_dir : Path | None
        Directory for verified release archives. Defaults to `.cache/z3`
        below the repository root.
    """

    repo_root: Path
    install_dir: Path | None = None
    cache_dir: Path | None = None


def default_z3_install_dir(repo_root: Path) -> Path:
    """Return the versioned directory that holds the `z3` executable."""
    return default_z3_cache_dir(repo_root) / Z3_VERSION


def default_z3_cache_dir(repo_root: Path) -> Path:
    """Return the default Z3 archive cache directory.

    Parameters
    ----------
    repo_root : Path
        Repository root.

    Returns
    -------
    Path
        `.cache/z3` below the repository root.
    """
    return repo_root / ".cache" / "z3"


def install_z3(options: Z3InstallOptions) -> tuple[str, ...]:
    """Install the pinned Z3 solver and return user-facing output lines.

    Parameters
    ----------
    options : Z3InstallOptions
        Repository root and optional install and cache directories.

    Returns
    -------
    tuple[str, ...]
        Status line and the `VERUS_Z3_PATH` export instruction.

    Raises
    ------
    ProverToolError
        Raised for an unsupported host, a download limit, an invalid archive,
        a missing member, or a filesystem failure.
    """
    require_linux_x86_64("Z3")
    install_dir = resolve_configured_path(
        options.install_dir,
        options.repo_root,
        default_z3_install_dir(options.repo_root),
    )
    cache_dir = resolve_configured_path(
        options.cache_dir,
        options.repo_root,
        default_z3_cache_dir(options.repo_root),
    )
    binary = install_dir / "z3"
    marker = install_dir / MARKER_NAME
    if _is_installed(binary, marker):
        return (
            f"Z3 {Z3_VERSION} already installed at {binary}",
            f"Export VERUS_Z3_PATH={binary}",
        )

    archive = download_pinned(Z3_ASSET, cache_dir / Z3_ASSET.name)
    _extract_z3(archive, install_dir, binary)
    _write_marker(marker)
    return (
        f"Installed Z3 {Z3_VERSION} at {binary}",
        f"Export VERUS_Z3_PATH={binary}",
    )


def _is_installed(binary: Path, marker: Path) -> bool:
    """Return whether the binary and its version marker are present."""
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == Z3_VERSION
    except OSError:
        return False


def _extract_z3(archive_path: Path, install_dir: Path, binary: Path) -> None:
    """Extract only the pinned `z3` member from the verified archive."""
    temporary = install_dir / ".z3.download"
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            member = _z3_member(archive)
            with archive.open(member) as source, temporary.open("wb") as output:
                written = 0
                while chunk := source.read(COPY_CHUNK_SIZE):
                    written += len(chunk)
                    if written > Z3_EXTRACTED_MAX_BYTES:
                        msg = (
                            f"Z3 archive member {Z3_MEMBER} exceeded the "
                            f"{Z3_EXTRACTED_MAX_BYTES}-byte extraction limit"
                        )
                        raise ProverToolError(msg)
                    output.write(chunk)
        temporary.chmod(0o755)
        temporary.replace(binary)
    except (OSError, zipfile.BadZipFile) as exc:
        msg = f"failed to extract Z3 archive '{archive_path}': {exc}"
        raise ProverToolError(msg) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _z3_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """Return the pinned Z3 executable member."""
    try:
        member = archive.getinfo(Z3_MEMBER)
    except KeyError as exc:
        msg = f"Z3 archive is missing {Z3_MEMBER}"
        raise ProverToolError(msg) from exc
    if member.is_dir():
        msg = f"Z3 archive entry {Z3_MEMBER} is a directory"
        raise ProverToolError(msg)
    if member.file_size > Z3_EXTRACTED_MAX_BYTES:
        msg = (
            f"Z3 archive entry {Z3_MEMBER} is {member.file_size} bytes, "
            f"above the {Z3_EXTRACTED_MAX_BYTES}-byte extraction limit"
        )
        raise ProverToolError(msg)
    return member


def _write_marker(marker: Path) -> None:
    """Record the installed version beside the binary."""
    temporary = marker.with_name(f".{marker.name}.download")
    try:
        temporary.write_text(f"{Z3_VERSION}\n", encoding="utf-8")
        temporary.replace(marker)
    except OSError as exc:
        msg = f"failed to record Z3 installation in '{marker}': {exc}"
        raise ProverToolError(msg) from exc
    finally:
        temporary.unlink(missing_ok=True)
