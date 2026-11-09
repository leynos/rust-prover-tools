"""Deterministic snapshot fixtures shared by unit and behavioural tests.

The values are synthetic but complete. They exercise the real parsers rather
than bypassing them, so a schema change breaks the fixtures instead of letting
tests drift from production.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import typing as typ

from rust_prover_tools.kani_snapshot.manifest import manifest_to_json
from rust_prover_tools.kani_snapshot.models import (
    AssetRecord,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    PrerequisiteRecord,
    ResourceObservation,
    RuntimeIdentity,
    SnapshotManifest,
    SnapshotPin,
    SolverRecord,
    SubmoduleRecord,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.pins import pin_to_toml

UPSTREAM_COMMIT: typ.Final[str] = "a" * 40
BUILDER_COMMIT: typ.Final[str] = "b" * 40
BACKEND_REVISION: typ.Final[str] = UPSTREAM_COMMIT
MANIFEST_SHA256: typ.Final[str] = "c" * 64
TAG: typ.Final[str] = "kani-snapshot-2026-09-aaaaaaaaaaaa-r1"
#: A second, distinct identity that reports the same upstream version.
OTHER_TAG: typ.Final[str] = "kani-snapshot-2026-09-bbbbbbbbbbbb-r2"
OTHER_UPSTREAM: typ.Final[str] = "b" * 40
OTHER_BUILDER: typ.Final[str] = "e" * 40
OTHER_MANIFEST_SHA256: typ.Final[str] = "f" * 64


def make_pin(**overrides: object) -> SnapshotPin:
    """Return a complete valid pin, applying overrides.

    Parameters
    ----------
    **overrides : object
        Field values replacing the defaults.

    Returns
    -------
    SnapshotPin
        Synthetic validated pin.
    """
    values: dict[str, object] = {
        "schema_version": 1,
        "repository": "leynos/rust-prover-tools",
        "tag": TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "builder_commit": BUILDER_COMMIT,
        "target": "x86_64-unknown-linux-gnu",
        "manifest_sha256": MANIFEST_SHA256,
    }
    values.update(overrides)
    return SnapshotPin(**typ.cast("dict[str, typ.Any]", values))


def other_pin(**overrides: object) -> SnapshotPin:
    """Return a second valid pin with a distinct complete identity."""
    return make_pin(
        tag=OTHER_TAG,
        upstream_commit=OTHER_UPSTREAM,
        builder_commit=OTHER_BUILDER,
        manifest_sha256=OTHER_MANIFEST_SHA256,
        **overrides,
    )


def make_assets() -> tuple[AssetRecord, ...]:
    """Return one synthetic asset per required role."""
    return (
        AssetRecord("backend", "backend.tar.gz", "application/gzip", 4096, "1" * 64),
        AssetRecord("launcher", "launcher.tar.gz", "application/gzip", 1024, "2" * 64),
        AssetRecord("notices", "notices.tar.gz", "application/gzip", 512, "3" * 64),
        AssetRecord("inventory", "inventory.json", "application/json", 256, "4" * 64),
    )


def make_manifest(**overrides: object) -> SnapshotManifest:
    """Return a complete valid manifest, applying overrides.

    Parameters
    ----------
    **overrides : object
        Field values replacing the defaults.

    Returns
    -------
    SnapshotManifest
        Synthetic validated manifest.
    """
    values: dict[str, object] = {
        "schema_version": 1,
        "repository": "leynos/rust-prover-tools",
        "tag": TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "builder_commit": BUILDER_COMMIT,
        "target": "x86_64-unknown-linux-gnu",
        "workflow": WorkflowIdentity(
            path=".github/workflows/kani-monthly.yml",
            event="schedule",
            ref="refs/heads/main",
            run_id=12345,
            run_attempt=1,
            run_url="https://github.com/leynos/rust-prover-tools/actions/runs/12345",
        ),
        "toolchain": ToolchainIdentity(
            identifier="nightly-2026-09-23",
            rustc_vv="rustc 1.100.0-nightly (6bb1652a0 2026-09-22)",
            kani_compiler="kani-compiler 0.67.0",
            components=("rust-src",),
        ),
        "launcher_package_version": "0.67.0",
        "backend": BackendIdentity(
            version="0.67.0",
            revision=BACKEND_REVISION,
            solvers=(
                SolverRecord("cbmc", "6.5.0", "5" * 64),
                SolverRecord("kissat", "4.0.1", "6" * 64),
            ),
        ),
        "runtime": RuntimeIdentity(
            glibc_baseline="2.35",
            cpu_baseline="x86-64-v2",
            dynamic_loader="/lib64/ld-linux-x86-64.so.2",
            shared_libraries=("libz.so.1",),
        ),
        "build": BuildInputs(
            submodules=(SubmoduleRecord("tests/perf/s2n-quic", "7" * 40),),
            lock_digests=(("Cargo.lock", "8" * 64),),
            recipe_digest="9" * 64,
        ),
        "prerequisites": (PrerequisiteRecord("cbmc", "apt:ubuntu", "6.5.0", "a" * 64),),
        "cache": CacheObservation("miss", "kani-nightly-2026-09-23"),
        "resources": ResourceObservation(
            wall_seconds=1800.0,
            disk_high_water_bytes=8_000_000_000,
            asset_bytes=5888,
        ),
        "assets": make_assets(),
    }
    values.update(overrides)
    return SnapshotManifest(**typ.cast("dict[str, typ.Any]", values))


def manifest_bytes(manifest: SnapshotManifest) -> bytes:
    """Render a manifest as canonical bytes."""
    return manifest_to_json(manifest)


def pin_bytes(pin: SnapshotPin) -> bytes:
    """Render a pin as canonical TOML bytes."""
    return pin_to_toml(pin)


def with_manifest(pin: SnapshotPin, manifest: SnapshotManifest) -> SnapshotPin:
    """Return `pin` naming the exact digest of `manifest`."""
    digest = hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    return dc.replace(pin, manifest_sha256=digest)
