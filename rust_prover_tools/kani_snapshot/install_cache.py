"""Installed-tree and cached-payload validation for snapshots.

Downloading and staging only prove that the release files agree with the
manifest. This module proves that the tree setup and launcher placement
produced, and the cached payloads a generation depends on, still agree too.
Activation and execution call it before trusting either.
"""

from __future__ import annotations

import hashlib
import tempfile
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive import extract_archive
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.install_transport import (
    asset_by_role,
    bundle_policy,
)
from rust_prover_tools.kani_snapshot.integrity import verify_asset
from rust_prover_tools.kani_snapshot.inventory import (
    load_inventory,
    validate_archive_inventory,
    validate_tree,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import (
        AssetRecord,
        SnapshotManifest,
    )
    from rust_prover_tools.kani_snapshot.snapshot_layout import (
        SnapshotLayout,
        SnapshotRuntime,
    )

__all__ = ("validate_cached_payloads", "validate_installed_tree")

_FULLY_VERIFIED_ROLES: typ.Final[frozenset[str]] = frozenset({
    "inventory",
    "launcher",
})
#: Members that setup or launcher placement add to the backend inventory.
_SETUP_ADDITIONS: typ.Final[tuple[str, ...]] = (
    "bin/cargo-kani",
    "bin/kani",
    "pyroot",
    "toolchain",
)
_READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024


def validate_installed_tree(
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> None:
    """Require installed backend and launcher bytes to be the published ones.

    Cache validation only proves the downloaded archives agree with the
    manifest. This check proves the tree setup and launcher placement produced
    still does, so activation and execution never run tampered bytes.

    Parameters
    ----------
    layout : SnapshotLayout
        Generation whose installed tree is inspected.
    manifest : SnapshotManifest
        Manifest publishing the inventory and launcher archive.
    runtime : SnapshotRuntime
        Resolved paths for the installed backend.

    Raises
    ------
    ProverToolError
        Raised when the installed tree differs from the inventory, or when an
        installed launcher file differs from the verified archive.
    """
    inventory_asset, _ = _payload_assets(manifest)
    inventory = load_inventory(layout.downloads_dir / inventory_asset.name)
    validate_tree(inventory, runtime.kani_dir, allowed_extra=_SETUP_ADDITIONS)
    _validate_installed_launcher(layout, manifest, runtime)


def _validate_installed_launcher(
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> None:
    """Compare installed launcher files with the digest-pinned archive."""
    archive = layout.downloads_dir / asset_by_role(manifest, "launcher").name
    with tempfile.TemporaryDirectory(
        prefix=".launcher-check-",
        dir=layout.generation_dir,
    ) as scratch:
        extracted = Path(scratch) / "launcher"
        extract_archive(
            archive,
            extracted,
            policy=bundle_policy(manifest.backend.version),
        )
        source = extracted / "bin"
        for entry in sorted(source.iterdir()):
            if not entry.is_file():
                continue
            installed = runtime.kani_dir / "bin" / entry.name
            if not _is_regular(installed) or _sha256_file(installed) != _sha256_file(
                entry
            ):
                msg = (
                    "snapshot installed launcher "
                    f"'{entry.name}' does not match the verified archive"
                )
                raise ProverToolError(msg)


def _sha256_file(path: Path) -> str:
    """Return the digest of one file, reporting read failures."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(_READ_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        msg = f"failed to read installed snapshot file '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return digest.hexdigest()


def validate_cached_payloads(
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
) -> None:
    """Recheck cached sizes, small payload digests and the backend inventory."""
    downloads = layout.downloads_dir
    for asset in manifest.assets:
        path = downloads / asset.name
        if not _is_regular(path):
            msg = f"snapshot cache is missing asset '{asset.name}'"
            raise ProverToolError(msg)
        if path.stat().st_size != asset.size:
            msg = f"snapshot cached asset '{asset.name}' has the wrong size"
            raise ProverToolError(msg)
        if asset.role in _FULLY_VERIFIED_ROLES:
            verify_asset(path, asset)
    inventory_asset, backend_asset = _payload_assets(manifest)
    inventory = load_inventory(downloads / inventory_asset.name)
    policy = bundle_policy(manifest.backend.version)
    entries = read_archive(downloads / backend_asset.name, policy=policy)
    validate_archive_inventory(entries, inventory)


def _payload_assets(manifest: SnapshotManifest) -> tuple[AssetRecord, AssetRecord]:
    """Return the inventory and backend records."""
    by_role = {asset.role: asset for asset in manifest.assets}
    return by_role["inventory"], by_role["backend"]


def _is_regular(path: Path) -> bool:
    """Return whether a path is an existing regular file."""
    try:
        return path.is_file()
    except OSError:
        return False
