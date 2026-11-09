"""Canonical JSON rendering of a validated snapshot manifest.

The producer renders with sorted keys and a fixed two-space indent so that
rerendering the same model is byte-stable. Rendering is kept separate from
parsing because the parser must never reserialise before the consumer's digest
check.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import AssetRecord, SnapshotManifest

__all__ = ("manifest_to_json",)


def _asset_document(asset: AssetRecord) -> dict[str, object]:
    """Render one asset record."""
    return {
        "role": asset.role,
        "name": asset.name,
        "media_type": asset.media_type,
        "size": asset.size,
        "sha256": asset.sha256,
    }


def _document(manifest: SnapshotManifest) -> dict[str, object]:
    """Render every manifest group as JSON-compatible values."""
    workflow = manifest.workflow
    toolchain = manifest.toolchain
    backend = manifest.backend
    runtime = manifest.runtime
    build = manifest.build
    return {
        "schema_version": manifest.schema_version,
        "repository": manifest.repository,
        "tag": manifest.tag,
        "upstream_commit": manifest.upstream_commit,
        "builder_commit": manifest.builder_commit,
        "target": manifest.target,
        "workflow": {
            "path": workflow.path,
            "event": workflow.event,
            "ref": workflow.ref,
            "run_id": workflow.run_id,
            "run_attempt": workflow.run_attempt,
            "run_url": workflow.run_url,
        },
        "toolchain": {
            "identifier": toolchain.identifier,
            "rustc_vv": toolchain.rustc_vv,
            "kani_compiler": toolchain.kani_compiler,
            "components": list(toolchain.components),
        },
        "launcher": {"package_version": manifest.launcher_package_version},
        "backend": {
            "version": backend.version,
            "revision": backend.revision,
            "solvers": [
                {
                    "name": solver.name,
                    "version": solver.version,
                    "sha256": solver.sha256,
                }
                for solver in backend.solvers
            ],
        },
        "runtime": {
            "glibc_baseline": runtime.glibc_baseline,
            "cpu_baseline": runtime.cpu_baseline,
            "dynamic_loader": runtime.dynamic_loader,
            "shared_libraries": list(runtime.shared_libraries),
        },
        "build": {
            "submodules": [
                {"path": item.path, "commit": item.commit} for item in build.submodules
            ],
            "lock_digests": dict(build.lock_digests),
            "recipe_digest": build.recipe_digest,
        },
        "prerequisites": [
            {
                "name": item.name,
                "source": item.source,
                "version": item.version,
                "sha256": item.sha256,
            }
            for item in manifest.prerequisites
        ],
        "cache": {"outcome": manifest.cache.outcome, "key": manifest.cache.key},
        "resources": {
            "wall_seconds": manifest.resources.wall_seconds,
            "disk_high_water_bytes": manifest.resources.disk_high_water_bytes,
            "asset_bytes": manifest.resources.asset_bytes,
        },
        "assets": [_asset_document(asset) for asset in manifest.assets],
    }


def manifest_to_json(manifest: SnapshotManifest) -> bytes:
    """Render a manifest as deterministic UTF-8 JSON.

    Parameters
    ----------
    manifest : SnapshotManifest
        Manifest to render.

    Returns
    -------
    bytes
        Sorted-key JSON with a final newline.
    """
    text = json.dumps(_document(manifest), sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")
