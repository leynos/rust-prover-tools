"""Installed-tree and cached-payload validation for snapshots.

Downloading and staging only prove that the release files agree with the
manifest. This module proves that the tree setup and launcher placement
produced, and the cached payloads a generation depends on, still agree too.
Activation and execution call it before trusting either.
"""

from __future__ import annotations

import hashlib
import os
import stat
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
    import collections.abc as cabc

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
    *,
    checkpoint: cabc.Callable[[], None] | None = None,
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
    checkpoint : collections.abc.Callable[[], None] | None
        Called while the tree and launcher digests are verified.

    Raises
    ------
    ProverToolError
        Raised when the installed tree differs from the inventory, the Python
        root or one of its links escapes the generation, or an installed
        launcher file differs from the verified archive.
    """
    inventory_asset, _ = _payload_assets(manifest)
    inventory = load_inventory(layout.downloads_dir / inventory_asset.name)
    validate_tree(
        inventory,
        runtime.kani_dir,
        allowed_extra=_SETUP_ADDITIONS,
        checkpoint=checkpoint,
    )
    _require_python_root(runtime, checkpoint)
    _validate_installed_launcher(layout, manifest, runtime, checkpoint)


def _require_python_root(
    runtime: SnapshotRuntime,
    checkpoint: cabc.Callable[[], None] | None,
) -> None:
    """Require the setup-created Python root to stay inside the generation.

    `pyroot` is an allowed extra because setup creates it, so the inventory
    cannot describe it. Execution nevertheless adds it to `PYTHONPATH` and
    `pyroot/bin` to `PATH`, which means a symbolic link or a relocated root
    could make the pinned launcher import or execute bytes outside the
    verified generation. Absence is accepted: a snapshot without a Python
    payload simply has no root to inspect.
    """
    root = runtime.python_root
    try:
        status = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        msg = f"failed to inspect snapshot Python root '{root}': {exc}"
        raise ProverToolError(msg) from exc
    if stat.S_ISLNK(status.st_mode):
        msg = f"snapshot Python root must not be a symbolic link: {root}"
        raise ProverToolError(msg)
    if not stat.S_ISDIR(status.st_mode):
        msg = f"snapshot Python root is not a directory: {root}"
        raise ProverToolError(msg)
    generation = _resolve(runtime.layout.generation_dir, "snapshot generation")
    resolved_root = _resolve(root, "snapshot Python root")
    if not resolved_root.is_relative_to(generation):
        msg = f"snapshot Python root resolves outside its generation: {root}"
        raise ProverToolError(msg)
    _require_contained_python_links(root, generation, checkpoint)


def _require_contained_python_links(
    root: Path,
    generation: Path,
    checkpoint: cabc.Callable[[], None] | None,
) -> None:
    """Reject any symbolic link below the Python root that escapes it."""
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in (*names, *filenames):
            if checkpoint is not None:
                checkpoint()
            path = Path(directory) / name
            try:
                status = path.lstat()
            except OSError as exc:
                msg = f"failed to inspect snapshot Python root member '{path}': {exc}"
                raise ProverToolError(msg) from exc
            if not stat.S_ISLNK(status.st_mode):
                continue
            target = _resolve(path, "snapshot Python root link")
            if not target.is_relative_to(generation):
                msg = f"snapshot Python root link escapes its generation: {path}"
                raise ProverToolError(msg)


def _resolve(path: Path, description: str) -> Path:
    """Resolve one path, reporting unreadable or looping links."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        msg = f"failed to resolve {description} '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _validate_installed_launcher(
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
    checkpoint: cabc.Callable[[], None] | None,
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
            checkpoint=checkpoint,
        )
        source = extracted / "bin"
        for entry in sorted(source.iterdir()):
            if checkpoint is not None:
                checkpoint()
            if not entry.is_file():
                continue
            installed = runtime.kani_dir / "bin" / entry.name
            if not _is_regular(installed) or _sha256_file(
                installed, checkpoint
            ) != _sha256_file(entry, checkpoint):
                msg = (
                    "snapshot installed launcher "
                    f"'{entry.name}' does not match the verified archive"
                )
                raise ProverToolError(msg)


def _sha256_file(
    path: Path,
    checkpoint: cabc.Callable[[], None] | None = None,
) -> str:
    """Return the digest of one file, reporting read failures."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(_READ_CHUNK_SIZE):
                if checkpoint is not None:
                    checkpoint()
                digest.update(chunk)
    except OSError as exc:
        msg = f"failed to read installed snapshot file '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return digest.hexdigest()


def validate_cached_payloads(
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    *,
    checkpoint: cabc.Callable[[], None] | None = None,
) -> None:
    """Recheck cached sizes, small payload digests and the backend inventory."""
    downloads = layout.downloads_dir
    for asset in manifest.assets:
        if checkpoint is not None:
            checkpoint()
        path = downloads / asset.name
        if not _is_regular(path):
            msg = f"snapshot cache is missing asset '{asset.name}'"
            raise ProverToolError(msg)
        if path.stat().st_size != asset.size:
            msg = f"snapshot cached asset '{asset.name}' has the wrong size"
            raise ProverToolError(msg)
        if asset.role in _FULLY_VERIFIED_ROLES:
            verify_asset(path, asset, checkpoint=checkpoint)
    inventory_asset, backend_asset = _payload_assets(manifest)
    inventory = load_inventory(downloads / inventory_asset.name)
    policy = bundle_policy(manifest.backend.version)
    entries = read_archive(
        downloads / backend_asset.name,
        policy=policy,
        checkpoint=checkpoint,
    )
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
