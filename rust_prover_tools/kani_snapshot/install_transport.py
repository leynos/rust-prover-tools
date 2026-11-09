"""Bounded local transport staging and archive/inventory agreement.

Every release file is copied into the identity's private downloads directory
through an exclusive pending file, under the size rule the reviewed manifest
states, before any other byte is trusted. The backend archive must then match
the published inventory structurally, so setup consumes exactly the tree the
producer described.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import os
import shutil
import stat
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import ArchivePolicy
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.install_model import (
    ATTESTATION_ASSET_NAME,
    ATTESTATION_MAX_BYTES,
    CHECKSUMS_ASSET_NAME,
    CHECKSUMS_MAX_BYTES,
    MANIFEST_ASSET_NAME,
    MANIFEST_MAX_BYTES,
    InstallHooks,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.integrity import (
    policy_for_pin,
    verify_asset,
    verify_checksums,
    verify_manifest_digest,
)
from rust_prover_tools.kani_snapshot.inventory import (
    load_inventory,
    validate_archive_inventory,
)
from rust_prover_tools.kani_snapshot.manifest import (
    parse_manifest,
    validate_binding,
)
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import (
        AssetRecord,
        SnapshotManifest,
    )
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = ("asset_by_role", "bundle_policy", "stage_assets")

_LOGGER = logging.getLogger(__name__)
_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
_READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024


@dc.dataclass(frozen=True, slots=True)
class _TransportLimit:
    """The size rule for one local transport file."""

    max_bytes: int
    exact_size: int | None = None


def stage_assets(
    options: InstallOptions,
    layout: SnapshotLayout,
    hooks: InstallHooks,
    *,
    check_deadline: cabc.Callable[[str], None],
) -> SnapshotManifest:
    """Copy and verify every transport file into the identity's downloads.

    Parameters
    ----------
    options : InstallOptions
        Reviewed pin and local asset directory.
    layout : SnapshotLayout
        Identity whose private downloads directory receives the files.
    hooks : InstallHooks
        Attestation verifier.
    check_deadline : collections.abc.Callable of (str) to None
        Raises when the installation budget has expired for a phase.

    Returns
    -------
    SnapshotManifest
        Manifest that every staged payload was verified against.

    Raises
    ------
    ProverToolError
        Raised when the transport is absent or any file fails its digest,
        size, checksum, attestation or structural agreement.
    """
    source = options.assets_dir
    if source is None:
        msg = "snapshot installation requires --asset-dir until remote transport exists"
        raise ProverToolError(msg)
    if not source.is_dir():
        msg = f"snapshot asset directory does not exist: {source}"
        raise ProverToolError(msg)
    downloads = layout.downloads_dir
    try:
        downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"failed to create snapshot downloads directory '{downloads}': {exc}"
        raise ProverToolError(msg) from exc
    manifest_path = _copy_local(
        source,
        downloads,
        MANIFEST_ASSET_NAME,
        _TransportLimit(MANIFEST_MAX_BYTES),
    )
    check_deadline("manifest verification")
    parsed = parse_manifest(manifest_path.read_bytes())
    verify_manifest_digest(parsed, options.pin)
    validate_binding(options.pin, parsed.manifest)
    attestation = _copy_local(
        source,
        downloads,
        ATTESTATION_ASSET_NAME,
        _TransportLimit(ATTESTATION_MAX_BYTES),
    )
    checksums = _copy_local(
        source,
        downloads,
        CHECKSUMS_ASSET_NAME,
        _TransportLimit(CHECKSUMS_MAX_BYTES),
    )
    verify_checksums(checksums, parsed.manifest, parsed.sha256)
    hooks.verify_attestation(policy_for_pin(options.pin), manifest_path, attestation)
    check_deadline("payload verification")
    for asset in parsed.manifest.assets:
        path = _copy_asset(source, downloads, asset)
        verify_asset(path, asset)
    _verify_bundle_structures(parsed.manifest, downloads)
    log_event(
        _LOGGER,
        "install.staged",
        tag=options.pin.tag,
        assets=len(parsed.manifest.assets),
    )
    return parsed.manifest


def bundle_policy(version: str) -> ArchivePolicy:
    """Return the strip-and-root policy producer bundles use."""
    return ArchivePolicy(strip_components=1, required_root=f"kani-{version}")


def _copy_asset(source: Path, downloads: Path, asset: AssetRecord) -> Path:
    """Copy one manifest payload after checking its local size."""
    return _copy_local(
        source,
        downloads,
        asset.name,
        _TransportLimit(asset.size, exact_size=asset.size),
    )


def _copy_local(
    source: Path,
    destination_dir: Path,
    name: str,
    limit: _TransportLimit,
) -> Path:
    """Copy one local release file into the private downloads directory."""
    origin = source / name
    try:
        status = origin.stat()
    except OSError as exc:
        msg = f"failed to read snapshot asset '{name}': {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot asset '{name}' is not a regular file"
        raise ProverToolError(msg)
    if status.st_size > limit.max_bytes:
        msg = f"snapshot asset '{name}' exceeds its transport limit"
        raise ProverToolError(msg)
    if limit.exact_size is not None and status.st_size != limit.exact_size:
        msg = f"snapshot asset '{name}' has the wrong size"
        raise ProverToolError(msg)
    target = destination_dir / name
    pending = destination_dir / f".{name}.{os.getpid()}.pending"
    try:
        with origin.open("rb") as reader, pending.open("xb") as writer:
            shutil.copyfileobj(reader, writer, _READ_CHUNK_SIZE)
            writer.flush()
            os.fsync(writer.fileno())
        pending.chmod(_PRIVATE_FILE_MODE)
        pending.replace(target)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to stage snapshot asset '{name}': {exc}"
        raise ProverToolError(msg) from exc
    return target


def _verify_bundle_structures(manifest: SnapshotManifest, downloads: Path) -> None:
    """Require the backend archive to match the published inventory."""
    inventory = load_inventory(downloads / asset_by_role(manifest, "inventory").name)
    policy = bundle_policy(manifest.backend.version)
    entries = read_archive(
        downloads / asset_by_role(manifest, "backend").name,
        policy=policy,
    )
    validate_archive_inventory(entries, inventory)
    read_archive(downloads / asset_by_role(manifest, "launcher").name, policy=policy)


def asset_by_role(manifest: SnapshotManifest, role: str) -> AssetRecord:
    """Return the single asset for one required role."""
    matches = [asset for asset in manifest.assets if asset.role == role]
    if len(matches) != 1:
        msg = f"snapshot manifest must publish exactly one '{role}' asset"
        raise ProverToolError(msg)
    return matches[0]
