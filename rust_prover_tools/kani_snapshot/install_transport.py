"""Bounded local transport staging and archive/inventory agreement.

Every release file is copied into the identity's private downloads directory
through an exclusive pending file, under the size rule the reviewed manifest
states, before any other byte is trusted. The backend archive must then match
the published inventory structurally, so setup consumes exactly the tree the
producer described.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import logging
import os
import secrets
import shutil
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import ArchivePolicy
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.install_copy import (
    TransportFile,
    TransportLimit,
)
from rust_prover_tools.kani_snapshot.install_copy import (
    copy_local as _copy_local,
)
from rust_prover_tools.kani_snapshot.install_copy import (
    transport_file as _transport_file,
)
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
from rust_prover_tools.kani_snapshot.publication import Publication, publish_staged
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import (
        AssetRecord,
        SnapshotManifest,
    )
    from rust_prover_tools.kani_snapshot.progress import Progress
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = ("asset_by_role", "bundle_policy", "stage_assets")

_LOGGER = logging.getLogger(__name__)
_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700

#: Design envelopes for one compressed payload and for the inventory document.
PAYLOAD_MAX_BYTES: typ.Final[int] = 2 * 1024 * 1024 * 1024
INVENTORY_MAX_BYTES: typ.Final[int] = 64 * 1024 * 1024


@dc.dataclass(frozen=True, slots=True)
class _Checkpoints:
    """Deadline checkpoints for the transport's long phases.

    Each phase name is separate so an expiry reports which piece of staging
    was interrupted; grouping them keeps `stage_assets` below the local-variable
    ceiling and names the set as one concept.
    """

    staging: Progress
    payload: Progress
    digest: Progress
    archive: Progress
    publish: Progress

    @classmethod
    def for_budget(cls, budget: InstallBudget) -> _Checkpoints:
        """Bind every phase name to the shared installation budget."""
        return cls(
            staging=functools.partial(budget.check, "transport staging"),
            payload=functools.partial(budget.check, "payload staging"),
            digest=functools.partial(budget.check, "payload verification"),
            archive=functools.partial(
                budget.check,
                "archive structure validation",
            ),
            publish=functools.partial(budget.check, "payload publication"),
        )


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
    staging = _new_staging_directory(layout, budget)
    names = [MANIFEST_ASSET_NAME, ATTESTATION_ASSET_NAME, CHECKSUMS_ASSET_NAME]
    checkpoints = _Checkpoints.for_budget(budget)
    try:
        manifest_path = budget.supervise(
            "transport staging",
            functools.partial(
                _copy_local,
                source,
                staging,
                _transport_file(MANIFEST_ASSET_NAME, MANIFEST_MAX_BYTES),
                checkpoints.staging,
            ),
        )
        budget.check("manifest verification")
        parsed = parse_manifest(manifest_path.read_bytes())
        verify_manifest_digest(parsed, options.pin)
        validate_binding(options.pin, parsed.manifest)
        attestation = budget.supervise(
            "transport staging",
            functools.partial(
                _copy_local,
                source,
                staging,
                _transport_file(ATTESTATION_ASSET_NAME, ATTESTATION_MAX_BYTES),
                checkpoints.staging,
            ),
        )
        checksums = budget.supervise(
            "transport staging",
            functools.partial(
                _copy_local,
                source,
                staging,
                _transport_file(CHECKSUMS_ASSET_NAME, CHECKSUMS_MAX_BYTES),
                checkpoints.staging,
            ),
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
            path = budget.supervise(
                "payload staging",
                functools.partial(
                    _copy_asset,
                    source,
                    staging,
                    asset,
                    checkpoints.payload,
                ),
            )
            budget.supervise(
                "payload verification",
                functools.partial(
                    verify_asset,
                    path,
                    asset,
                    checkpoint=checkpoints.digest,
                ),
            )
            names.append(asset.name)
        budget.supervise(
            "archive structure validation",
            functools.partial(
                _verify_bundle_structures,
                parsed.manifest,
                staging,
                checkpoint=checkpoints.archive,
            ),
        )
        # Only fully verified evidence is published. A failed repair therefore
        # leaves the previously staged payloads and the active pointer alone.
        # The marker makes a publication that was not confirmed visible to
        # readers, so an abandoned replacement can never pass for a complete
        # cache.
        publish_staged(
            _publication(layout, staging, names),
            budget=budget,
            checkpoint=checkpoints.publish,
        )
    finally:
        _discard_staging(staging, budget)
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


def _copy_asset(
    source: Path,
    staging: Path,
    asset: AssetRecord,
    checkpoint: Progress | None = None,
) -> Path:
    """Copy one manifest payload under its design envelope.

    The manifest's declared size is exact, but it is not the only bound: the
    inventory document and every other payload also have design ceilings that
    a compromised manifest cannot raise.
    """
    envelope = INVENTORY_MAX_BYTES if asset.role == "inventory" else PAYLOAD_MAX_BYTES
    return _copy_local(
        source,
        staging,
        TransportFile(
            name=asset.name,
            limit=TransportLimit(min(asset.size, envelope), exact_size=asset.size),
        ),
        checkpoint,
    )


def _verify_bundle_structures(
    manifest: SnapshotManifest,
    downloads: Path,
    *,
    checkpoint: Progress | None = None,
) -> None:
    """Require the backend archive to match the published inventory."""
    inventory = load_inventory(downloads / asset_by_role(manifest, "inventory").name)
    policy = bundle_policy(manifest.backend.version)
    entries = read_archive(
        downloads / asset_by_role(manifest, "backend").name,
        policy=policy,
        checkpoint=checkpoint,
    )
    validate_archive_inventory(entries, inventory)
    read_archive(
        downloads / asset_by_role(manifest, "launcher").name,
        policy=policy,
        checkpoint=checkpoint,
    )


def asset_by_role(manifest: SnapshotManifest, role: str) -> AssetRecord:
    """Return the single asset for one required role."""
    matches = [asset for asset in manifest.assets if asset.role == role]
    if len(matches) != 1:
        msg = f"snapshot manifest must publish exactly one '{role}' asset"
        raise ProverToolError(msg)
    return matches[0]


def _publication(
    layout: SnapshotLayout,
    staging: Path,
    names: list[str],
) -> Publication:
    """Own a fresh marker and every marker left by earlier publications."""
    publication_id = f"{os.getpid():x}-{secrets.token_hex(8)}"
    marker = layout.publication_marker(publication_id)
    return Publication(
        staging=staging,
        downloads=layout.downloads_dir,
        marker=marker,
        stale_markers=tuple(
            path for path in layout.publication_markers() if path != marker
        ),
        names=tuple(names),
    )


def _new_staging_directory(layout: SnapshotLayout, budget: InstallBudget) -> Path:
    """Return a fresh private staging directory beside the downloads.

    The directory is private and uniquely named, so a creation that outlives
    the budget may be abandoned: it exposes nothing, and a later install uses
    a different name.
    """
    staging = layout.identity_dir / f".staging-{os.getpid()}-{secrets.token_hex(8)}"

    def create() -> None:
        """Create the private staging directory."""
        staging.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=False)

    try:
        budget.supervise("staging directory creation", create)
    except OSError as exc:
        msg = f"failed to create snapshot staging directory '{staging}': {exc}"
        raise ProverToolError(msg) from exc
    return staging


def _discard_staging(staging: Path, budget: InstallBudget) -> None:
    """Remove the private staging tree without holding the caller past budget.

    Cleanup touches only this run's uniquely named directory, so a removal
    that outlives the budget may be abandoned: it cannot disturb a later
    install. A deferred or failed removal is logged rather than reported,
    because the verified installation has already been decided by then.
    """

    def remove() -> None:
        """Remove the private staging tree."""
        shutil.rmtree(staging, ignore_errors=True)

    try:
        budget.supervise("staging cleanup", remove)
    except ProverToolError as exc:
        _LOGGER.debug("snapshot staging cleanup deferred for %s: %s", staging.name, exc)
