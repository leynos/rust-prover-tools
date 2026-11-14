"""Bounded local transport staging and archive/inventory agreement.

Every release file is copied into the identity's private downloads directory
through an exclusive pending file, under the reviewed manifest's size rule,
before any other byte is trusted. The backend archive must then match the
published inventory structurally, so setup consumes exactly the described tree.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import logging
import os
import secrets
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.activation import highest_sequence
from rust_prover_tools.kani_snapshot.archive_graph import ArchivePolicy
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.attestation import (
    AttestationRequest,
    policy_for_pin,
)
from rust_prover_tools.kani_snapshot.cleanup import (
    StagingClaim,
    discard_private_tree,
    new_staging_directory,
    sweep_deferred_staging,
)
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
    ClaimedBudget,
    InstallBudget,
    InstallHooks,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.integrity import (
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

#: Design envelopes for one compressed payload and for the inventory document.
PAYLOAD_MAX_BYTES: typ.Final[int] = 2 * 1024 * 1024 * 1024
INVENTORY_MAX_BYTES: typ.Final[int] = 64 * 1024 * 1024


@dc.dataclass(frozen=True, slots=True)
class _Checkpoints:
    """Deadline checkpoints for the transport's long phases.

    One name per phase keeps `stage_assets` within its local-variable limit.
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
        Shared installation budget; attestation verification is bounded by
        what remains so an unresponsive `gh` cannot outlive it.

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
    # A hung probe must not hold the identity lock, and abandoning a read is safe.
    if not budget.supervise("asset directory check", source.is_dir):
        msg = f"snapshot asset directory does not exist: {source}"
        raise ProverToolError(msg)
    sweep_deferred_staging(layout.identity_dir, budget)
    staging = new_staging_directory(layout.identity_dir, budget)
    # Supervised work is abandoned, not stopped; this budget holds the claim for it.
    claimed = _claim_staging(staging, budget)
    checkpoints = _Checkpoints.for_budget(budget)
    try:
        manifest_path = claimed.supervise(
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
        # Reading the staged manifest is blocking work on a shared budget;
        # abandoning a read mutates nothing, so it is supervised.
        parsed = parse_manifest(
            claimed.supervise(
                "manifest read",
                manifest_path.read_bytes,
            ),
        )
        verify_manifest_digest(parsed, options.pin)
        validate_binding(options.pin, parsed.manifest)
        attestation = claimed.supervise(
            "transport staging",
            functools.partial(
                _copy_local,
                source,
                staging,
                _transport_file(ATTESTATION_ASSET_NAME, ATTESTATION_MAX_BYTES),
                checkpoints.staging,
            ),
        )
        checksums = claimed.supervise(
            "transport staging",
            functools.partial(
                _copy_local,
                source,
                staging,
                _transport_file(CHECKSUMS_ASSET_NAME, CHECKSUMS_MAX_BYTES),
                checkpoints.staging,
            ),
        )
        claimed.supervise(
            "checksum verification",
            functools.partial(
                verify_checksums,
                checksums,
                parsed.manifest,
                parsed.sha256,
            ),
        )
        hooks.verify_attestation(
            AttestationRequest(
                policy_for_pin(options.pin),
                manifest_path,
                attestation,
                parsed.manifest.workflow,
            ),
            timeout_seconds=budget.remaining(),
        )
        budget.check("payload verification")
        names = _stage_payloads(source, staging, parsed.manifest, claimed)
        claimed.supervise(
            "archive structure validation",
            functools.partial(
                _verify_bundle_structures,
                parsed.manifest,
                staging,
                checkpoint=checkpoints.archive,
            ),
        )
        # Publishing is the last step, so only fully verified evidence reaches the
        # cache; the marker makes an abandoned replacement visibly incomplete.
        publish_staged(
            _publication(layout, staging, names, claimed),
            budget=claimed,
            checkpoint=checkpoints.publish,
        )
    except BaseException:
        # Only a claim no worker holds lets this run remove the tree; an
        # abandoned worker releases it when it stops for a later sweep.
        if claimed.finish():
            discard_private_tree(staging, budget=budget, phase="staging cleanup")
        raise
    if claimed.finish():
        discard_private_tree(staging, budget=budget, phase="staging cleanup")
    # The snapshot tag is omitted: it changes per release and would grow a
    # metric label without bound.
    log_event(
        _LOGGER,
        "kani.install.staged",
        assets=len(parsed.manifest.assets),
    )
    return parsed.manifest


def _claim_staging(staging: Path, budget: InstallBudget) -> ClaimedBudget:
    """Claim a staging tree and return a budget that keeps it held.

    The claim is taken under the budget, and one that finishes after that
    deadline closes the descriptor it opened, so cancelling cannot leak it.
    """
    claim = StagingClaim()
    try:
        budget.supervise(
            "staging ownership claim",
            functools.partial(claim.take, staging),
        )
    except BaseException:
        claim.cancel()
        raise
    return ClaimedBudget(budget, claim)


def bundle_policy(version: str) -> ArchivePolicy:
    """Return the strip-and-root policy producer bundles use."""
    return ArchivePolicy(strip_components=1, required_root=f"kani-{version}")


def _stage_payloads(
    source: Path,
    staging: Path,
    manifest: SnapshotManifest,
    budget: InstallBudget,
) -> tuple[str, ...]:
    """Copy and verify every manifest payload, naming what was published."""
    checkpoints = _Checkpoints.for_budget(budget)
    names = [MANIFEST_ASSET_NAME, ATTESTATION_ASSET_NAME, CHECKSUMS_ASSET_NAME]
    for asset in manifest.assets:
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
    return tuple(names)


def _copy_asset(
    source: Path,
    staging: Path,
    asset: AssetRecord,
    checkpoint: Progress | None = None,
) -> Path:
    """Copy one manifest payload under its design envelope.

    The declared size is exact but not the only bound: the inventory document
    and every payload also have ceilings a compromised manifest cannot raise.
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
    names: tuple[str, ...],
    budget: InstallBudget,
) -> Publication:
    """Own a fresh marker and every marker left by earlier publications.

    The marker records the highest activation sequence visible now: the base a
    later completion must exceed to retire it. A publication resumed after a
    later installation completed writes a lower base, so readers see it was
    overtaken. The base comes from record names, not the watermark, so a late
    completion cannot change what this publication observed. Both listings are
    reads of shared state, so each is supervised.
    """
    publication_id = f"{os.getpid():x}-{secrets.token_hex(8)}"
    marker = layout.publication_marker(publication_id)
    base = budget.supervise(
        "activation sequence read",
        functools.partial(highest_sequence, layout),
    )
    published = budget.supervise(
        "publication marker listing",
        layout.publication_markers,
    )
    return Publication(
        staging=staging,
        downloads=layout.downloads_dir,
        marker=marker,
        stale_markers=tuple(path for path in published if path != marker),
        names=names,
        base_sequence=base,
    )
