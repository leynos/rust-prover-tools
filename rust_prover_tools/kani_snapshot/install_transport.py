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
import secrets
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
    InstallBudget,
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
_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700
_READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024

#: Design envelopes for one compressed payload and for the inventory document.
PAYLOAD_MAX_BYTES: typ.Final[int] = 2 * 1024 * 1024 * 1024
INVENTORY_MAX_BYTES: typ.Final[int] = 64 * 1024 * 1024


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
    budget: InstallBudget,
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
    budget : InstallBudget
        Shared installation budget. Its check raises once a phase starts too
        late, and attestation verification is bounded by what remains so an
        unresponsive `gh` cannot outlive the installation.

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
    staging = _new_staging_directory(layout)
    names = [MANIFEST_ASSET_NAME, ATTESTATION_ASSET_NAME, CHECKSUMS_ASSET_NAME]
    try:
        manifest_path = _copy_local(
            source,
            staging,
            MANIFEST_ASSET_NAME,
            _TransportLimit(MANIFEST_MAX_BYTES),
        )
        budget.check("manifest verification")
        parsed = parse_manifest(manifest_path.read_bytes())
        verify_manifest_digest(parsed, options.pin)
        validate_binding(options.pin, parsed.manifest)
        attestation = _copy_local(
            source,
            staging,
            ATTESTATION_ASSET_NAME,
            _TransportLimit(ATTESTATION_MAX_BYTES),
        )
        checksums = _copy_local(
            source,
            staging,
            CHECKSUMS_ASSET_NAME,
            _TransportLimit(CHECKSUMS_MAX_BYTES),
        )
        verify_checksums(checksums, parsed.manifest, parsed.sha256)
        hooks.verify_attestation(
            policy_for_pin(options.pin),
            manifest_path,
            attestation,
            timeout_seconds=budget.remaining(),
        )
        budget.check("payload verification")
        for asset in parsed.manifest.assets:
            path = _copy_asset(source, staging, asset)
            verify_asset(path, asset)
            names.append(asset.name)
        _verify_bundle_structures(parsed.manifest, staging)
        # Only fully verified evidence is published. A failed repair therefore
        # leaves the previously staged payloads and the active pointer alone.
        _publish_staged(staging, layout.downloads_dir, names)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log_event(
        _LOGGER,
        "kani.install.staged",
        tag=options.pin.tag,
        assets=len(parsed.manifest.assets),
    )
    return parsed.manifest


def bundle_policy(version: str) -> ArchivePolicy:
    """Return the strip-and-root policy producer bundles use."""
    return ArchivePolicy(strip_components=1, required_root=f"kani-{version}")


def _copy_asset(source: Path, staging: Path, asset: AssetRecord) -> Path:
    """Copy one manifest payload under its design envelope.

    The manifest's declared size is exact, but it is not the only bound: the
    inventory document and every other payload also have design ceilings that
    a compromised manifest cannot raise.
    """
    envelope = INVENTORY_MAX_BYTES if asset.role == "inventory" else PAYLOAD_MAX_BYTES
    return _copy_local(
        source,
        staging,
        asset.name,
        _TransportLimit(min(asset.size, envelope), exact_size=asset.size),
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
            written = _stream_bounded(reader, writer, name, limit)
            writer.flush()
            os.fsync(writer.fileno())
        pending.chmod(_PRIVATE_FILE_MODE)
        pending.replace(target)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to stage snapshot asset '{name}': {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        pending.unlink(missing_ok=True)
        raise
    if limit.exact_size is not None and written != limit.exact_size:
        target.unlink(missing_ok=True)
        msg = f"snapshot asset '{name}' has the wrong size"
        raise ProverToolError(msg)
    return target


def _stream_bounded(
    reader: typ.BinaryIO,
    writer: typ.BinaryIO,
    name: str,
    limit: _TransportLimit,
) -> int:
    """Copy until EOF, stopping at the transport bound.

    The pre-copy size check is only a fast path: a file that grows while it is
    copied must also stop at the bound instead of filling the cache.
    """
    written = 0
    while chunk := reader.read(_READ_CHUNK_SIZE):
        written += len(chunk)
        if written > limit.max_bytes:
            msg = f"snapshot asset '{name}' exceeds its transport limit"
            raise ProverToolError(msg)
        writer.write(chunk)
    return written


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


def _new_staging_directory(layout: SnapshotLayout) -> Path:
    """Return a fresh private staging directory beside the downloads."""
    staging = layout.identity_dir / f".staging-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        staging.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=False)
    except OSError as exc:
        msg = f"failed to create snapshot staging directory '{staging}': {exc}"
        raise ProverToolError(msg) from exc
    return staging


def _publish_staged(
    staging: Path,
    downloads: Path,
    names: cabc.Iterable[str],
) -> None:
    """Move verified staging files into the downloads directory."""
    try:
        downloads.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        for name in names:
            (staging / name).replace(downloads / name)
    except OSError as exc:
        msg = f"failed to publish snapshot payloads to '{downloads}': {exc}"
        raise ProverToolError(msg) from exc
