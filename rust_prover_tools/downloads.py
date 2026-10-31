"""Bounded, verified HTTPS downloads for pinned prover binaries.

The downloader accepts only `https` URLs, follows a bounded number of
redirects, rejects a `Content-Length` larger than the asset's ceiling before
reading the body, and counts streamed bytes so a response without a length
header cannot fill the disk. A monotonic transfer deadline covers connection,
redirects, and body transfer. Downloads are written to a pending file and
renamed only after the pinned SHA-256 digest matches, so a caller never sees a
partial asset at the final path.

The network-facing half of this policy lives in
`rust_prover_tools.download_transport`; this module adds digest pinning and the
atomic verified store.

Example
-------
```python
from pathlib import Path

from rust_prover_tools.downloads import PinnedAsset, download_pinned

asset = PinnedAsset(
    name="tool.tar.gz",
    url="https://example.test/tool.tar.gz",
    sha256="0" * 64,
    max_bytes=16 * 1024 * 1024,
)
download_pinned(asset, Path(".cache/tool.tar.gz"))
```
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import typing as typ

from rust_prover_tools.download_transport import (
    DEFAULT_DOWNLOAD_LIMITS,
    READ_CHUNK_SIZE,
    DownloadLimits,
    _download,
)
from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "DEFAULT_DOWNLOAD_LIMITS",
    "DownloadLimits",
    "PinnedAsset",
    "download_pinned",
    "sha256_file",
)


@dc.dataclass(frozen=True, slots=True)
class PinnedAsset:
    """A release asset with its trust and size policy.

    Attributes
    ----------
    name : str
        File name used for diagnostics and cache paths.
    url : str
        HTTPS URL for the asset.
    sha256 : str
        Expected lowercase hexadecimal SHA-256 digest.
    max_bytes : int
        Maximum accepted compressed response size.
    """

    name: str
    url: str
    sha256: str
    max_bytes: int


def download_pinned(
    asset: PinnedAsset,
    destination: Path,
    limits: DownloadLimits = DEFAULT_DOWNLOAD_LIMITS,
) -> Path:
    """Download a pinned asset and verify its SHA-256 digest.

    An existing destination is reused when its digest already matches. Any
    other destination is replaced atomically after a complete, verified
    download.

    Parameters
    ----------
    asset : PinnedAsset
        Asset URL, digest, and byte ceiling.
    destination : Path
        Final path for the verified asset.
    limits : DownloadLimits
        Network limits. Defaults to `DownloadLimits()`.

    Returns
    -------
    Path
        The verified destination path.

    Raises
    ------
    ProverToolError
        Raised when the URL, transfer, limits, digest, or filesystem operations
        fail.
    """
    _ensure_parent_directory(destination)

    if _has_verified_copy(asset, destination):
        return destination

    pending = destination.with_name(f"{destination.name}.download")
    try:
        _download_verified(asset, pending, limits)
        _publish_verified(asset, pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return destination


def _ensure_parent_directory(destination: Path) -> None:
    """Create the destination's parent directory, reporting OS errors."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"failed to create download directory '{destination.parent}': {exc}"
        raise ProverToolError(msg) from exc


def _has_verified_copy(asset: PinnedAsset, destination: Path) -> bool:
    """Return whether `destination` already holds the expected asset."""
    return destination.is_file() and sha256_file(destination) == asset.sha256


def _download_verified(
    asset: PinnedAsset,
    pending: Path,
    limits: DownloadLimits,
) -> None:
    """Download into `pending` and reject a digest that does not match."""
    _download(asset.url, pending, asset.max_bytes, limits)
    actual = sha256_file(pending)
    if actual != asset.sha256:
        msg = (
            f"SHA-256 mismatch for {asset.name}.\n"
            f"Expected: {asset.sha256}\n"
            f"Actual:   {actual}"
        )
        raise ProverToolError(msg)


def _publish_verified(asset: PinnedAsset, pending: Path, destination: Path) -> None:
    """Atomically move a verified pending file to its final path."""
    try:
        pending.replace(destination)
    except OSError as exc:
        msg = f"failed to store {asset.name} at '{destination}': {exc}"
        raise ProverToolError(msg) from exc


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of a file.

    Parameters
    ----------
    path : Path
        File to hash.

    Returns
    -------
    str
        Lowercase hexadecimal digest.

    Raises
    ------
    ProverToolError
        Raised when the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(READ_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        msg = f"failed to read '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return digest.hexdigest()
