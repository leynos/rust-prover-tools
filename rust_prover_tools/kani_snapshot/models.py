"""Frozen models for Kani snapshot pins and producer manifests.

The records here are the validated form of a consumer pin and a producer
manifest. Parsers in `pins` and `manifest` construct them from untrusted bytes;
callers must not build a `SnapshotManifest` from a partially checked document.
The deterministic cache key lives in `cache_key`.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_snapshot.toolchain_model import (
    ComponentRecord,
    FileRecord,
    ToolchainIdentity,
)

__all__ = (
    "ASSET_ROLES",
    "REPOSITORY",
    "SCHEMA_VERSION",
    "SUPPORTED_TARGET",
    "AssetRecord",
    "BackendIdentity",
    "BuildInputs",
    "CacheObservation",
    "ComponentRecord",
    "FileRecord",
    "PrerequisiteRecord",
    "ResourceObservation",
    "RuntimeIdentity",
    "SnapshotManifest",
    "SnapshotPin",
    "SolverRecord",
    "SubmoduleRecord",
    "ToolchainIdentity",
    "WorkflowIdentity",
)

#: The only repository that publishes these snapshots.
REPOSITORY: typ.Final[str] = "leynos/rust-prover-tools"

#: The single target supported by schema version 1.
SUPPORTED_TARGET: typ.Final[str] = "x86_64-unknown-linux-gnu"

#: The only schema version accepted by this implementation.
SCHEMA_VERSION: typ.Final[int] = 1

#: Payload roles required in every manifest. Each role has exactly one asset.
ASSET_ROLES: typ.Final[tuple[str, ...]] = (
    "backend",
    "launcher",
    "notices",
    "inventory",
)


@dc.dataclass(frozen=True, slots=True)
class SnapshotPin:
    """A reviewed consumer pin naming one immutable snapshot.

    Attributes
    ----------
    schema_version : int
        Pin schema version. Only version 1 is accepted.
    repository : str
        Publishing repository, exactly `leynos/rust-prover-tools`.
    tag : str
        Immutable release tag in the
        `kani-snapshot-YYYY-MM-<12-hex>-rN` grammar.
    upstream_commit : str
        Full 40-hex commit of `model-checking/kani`.
    builder_commit : str
        Full 40-hex commit of this repository that built the snapshot.
    target : str
        The single supported Rust target triple.
    manifest_sha256 : str
        Lowercase SHA-256 of the exact manifest bytes.
    """

    schema_version: int
    repository: str
    tag: str
    upstream_commit: str
    builder_commit: str
    target: str
    manifest_sha256: str


@dc.dataclass(frozen=True, slots=True)
class WorkflowIdentity:
    """The trusted workflow run that produced a snapshot.

    Attributes
    ----------
    path : str
        Workflow path that must match the attestation signer.
    event : str
        Event that started the run.
    ref : str
        Source ref, which must be `refs/heads/main`.
    run_id : int
        Positive workflow run identifier.
    run_attempt : int
        Positive workflow attempt number.
    run_url : str | None
        Optional recorded run URL.
    """

    path: str
    event: str
    ref: str
    run_id: int
    run_attempt: int
    run_url: str | None = None


@dc.dataclass(frozen=True, slots=True)
class SolverRecord:
    """One solver bundled with the backend and its executable digest.

    Attributes
    ----------
    name : str
        Solver name.
    version : str
        Solver version.
    sha256 : str
        Lowercase SHA-256 of the distributed executable.
    """

    name: str
    version: str
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class BackendIdentity:
    """The upstream backend and its solver inventory.

    Attributes
    ----------
    version : str
        Upstream Kani package version, preserved unchanged.
    revision : str
        Full upstream commit that the backend was built from.
    solvers : tuple[SolverRecord, ...]
        Bundled solvers and their executable digests.
    """

    version: str
    revision: str
    solvers: tuple[SolverRecord, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Measured runtime compatibility of the prebuilt payloads.

    Attributes
    ----------
    glibc_baseline : str
        Oldest glibc version the payloads were tested against.
    cpu_baseline : str | None
        Optional recorded x86-64 baseline.
    dynamic_loader : str | None
        Optional interpreter path the payloads require.
    shared_libraries : tuple[str, ...]
        Required shared libraries discovered during feasibility.
    """

    glibc_baseline: str
    cpu_baseline: str | None = None
    dynamic_loader: str | None = None
    shared_libraries: tuple[str, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class SubmoduleRecord:
    """One recursive source submodule and its recorded gitlink revision.

    Attributes
    ----------
    path : str
        Repository-relative submodule path.
    commit : str
        Full 40-hex gitlink commit.
    """

    path: str
    commit: str


@dc.dataclass(frozen=True, slots=True)
class PrerequisiteRecord:
    """One build prerequisite and its trusted provenance.

    Attributes
    ----------
    name : str
        Prerequisite name.
    source : str
        Trusted source URL or package name.
    version : str
        Recorded version.
    sha256 : str
        Lowercase SHA-256 of the distributed artifact.
    """

    name: str
    source: str
    version: str
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class BuildInputs:
    """Inputs whose equality decides whether a rebuild is required.

    Attributes
    ----------
    submodules : tuple[SubmoduleRecord, ...]
        Recursive submodule revisions in path order.
    lock_digests : tuple[tuple[str, str], ...]
        Recorded lockfile path and digest pairs.
    recipe_digest : str
        Digest of the recipe and build configuration.
    """

    submodules: tuple[SubmoduleRecord, ...]
    lock_digests: tuple[tuple[str, str], ...]
    recipe_digest: str


@dc.dataclass(frozen=True, slots=True)
class CacheObservation:
    """Recorded source-build cache outcome.

    Attributes
    ----------
    outcome : str
        Cache outcome label, such as `hit` or `miss`.
    key : str | None
        Optional cache key.
    """

    outcome: str
    key: str | None = None


@dc.dataclass(frozen=True, slots=True)
class ResourceObservation:
    """Measured build time, disk and asset sizes.

    Attributes
    ----------
    wall_seconds : float
        Wall-clock build time in seconds.
    disk_high_water_bytes : int
        Highest observed disk usage.
    asset_bytes : int
        Total size of the published payloads.
    """

    wall_seconds: float
    disk_high_water_bytes: int
    asset_bytes: int


@dc.dataclass(frozen=True, slots=True)
class AssetRecord:
    """One published payload and its exact byte identity.

    Attributes
    ----------
    role : str
        One of `ASSET_ROLES`.
    name : str
        Release asset basename.
    media_type : str
        Media type of the payload.
    size : int
        Exact size in bytes.
    sha256 : str
        Lowercase SHA-256 of the exact bytes.
    """

    role: str
    name: str
    media_type: str
    size: int
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """The validated manifest describing one complete snapshot.

    Attributes
    ----------
    schema_version : int
        Manifest schema version. Only version 1 is accepted.
    repository : str
        Publishing repository, exactly `leynos/rust-prover-tools`.
    tag : str
        Immutable release tag.
    upstream_commit : str
        Full upstream commit.
    builder_commit : str
        Full builder commit.
    target : str
        The single supported target triple.
    workflow : WorkflowIdentity
        Trusted workflow run identity.
    toolchain : ToolchainIdentity
        Exact compiler identities.
    launcher_package_version : str
        Version reported by the launcher package.
    backend : BackendIdentity
        Upstream backend identity.
    runtime : RuntimeIdentity
        Measured runtime compatibility.
    build : BuildInputs
        Build inputs that decide rebuilds.
    prerequisites : tuple[PrerequisiteRecord, ...]
        Build prerequisites and their provenance.
    cache : CacheObservation
        Recorded source-build cache outcome.
    resources : ResourceObservation
        Measured build resources.
    assets : tuple[AssetRecord, ...]
        Published payloads.
    """

    schema_version: int
    repository: str
    tag: str
    upstream_commit: str
    builder_commit: str
    target: str
    workflow: WorkflowIdentity
    toolchain: ToolchainIdentity
    launcher_package_version: str
    backend: BackendIdentity
    runtime: RuntimeIdentity
    build: BuildInputs
    prerequisites: tuple[PrerequisiteRecord, ...]
    cache: CacheObservation
    resources: ResourceObservation
    assets: tuple[AssetRecord, ...]
