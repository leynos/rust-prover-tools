"""Field-group parsers for the snapshot manifest schema.

Each parser owns one nested object from the version 1 manifest. They are kept
together so the accepted key sets, types and cross-field rules for a group can
be read and changed in one place. Top-level assembly lives in `manifest`.
"""

from __future__ import annotations

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import (
    ASSET_ROLES,
    AssetRecord,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    PrerequisiteRecord,
    ResourceObservation,
    RuntimeIdentity,
    SolverRecord,
    SubmoduleRecord,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.validation import (
    reject_unknown_keys,
    require_full_sha,
    require_mapping,
    require_non_negative_int,
    require_number,
    require_positive_int,
    require_sequence,
    require_sha256,
    require_truthy_text,
)

__all__ = (
    "parse_assets",
    "parse_backend",
    "parse_build",
    "parse_cache",
    "parse_prerequisite",
    "parse_resources",
    "parse_runtime",
    "parse_toolchain",
    "parse_workflow",
)

_WORKFLOW_KEYS = frozenset({
    "path",
    "event",
    "ref",
    "run_id",
    "run_attempt",
    "run_url",
})
_TOOLCHAIN_KEYS = frozenset({
    "identifier",
    "rustc_vv",
    "kani_compiler",
    "components",
})
_BACKEND_KEYS = frozenset({"version", "revision", "solvers"})
_SOLVER_KEYS = frozenset({"name", "version", "sha256"})
_RUNTIME_KEYS = frozenset({
    "glibc_baseline",
    "cpu_baseline",
    "dynamic_loader",
    "shared_libraries",
})
_BUILD_KEYS = frozenset({"submodules", "lock_digests", "recipe_digest"})
_SUBMODULE_KEYS = frozenset({"path", "commit"})
_PREREQUISITE_KEYS = frozenset({"name", "source", "version", "sha256"})
_CACHE_KEYS = frozenset({"outcome", "key"})
_RESOURCE_KEYS = frozenset({
    "wall_seconds",
    "disk_high_water_bytes",
    "asset_bytes",
})
_ASSET_KEYS = frozenset({"role", "name", "media_type", "size", "sha256"})


def _string_array(value: object, field: str) -> tuple[str, ...]:
    """Parse an array of non-empty strings."""
    items = require_sequence(value, field)
    return tuple(require_truthy_text(item, f"{field} item") for item in items)


def parse_workflow(document: dict[str, object], field: str) -> WorkflowIdentity:
    """Parse the workflow identity group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    WorkflowIdentity
        The validated workflow identity.
    """
    reject_unknown_keys(document, _WORKFLOW_KEYS, field)
    run_url = document.get("run_url")
    return WorkflowIdentity(
        path=require_truthy_text(document.get("path"), f"{field} path"),
        event=require_truthy_text(document.get("event"), f"{field} event"),
        ref=require_truthy_text(document.get("ref"), f"{field} ref"),
        run_id=require_positive_int(document.get("run_id"), f"{field} run_id"),
        run_attempt=require_positive_int(
            document.get("run_attempt"), f"{field} run_attempt"
        ),
        run_url=_optional_text(run_url, f"{field} run_url"),
    )


def parse_toolchain(document: dict[str, object], field: str) -> ToolchainIdentity:
    """Parse the toolchain identity group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    ToolchainIdentity
        The validated toolchain identity.
    """
    reject_unknown_keys(document, _TOOLCHAIN_KEYS, field)
    return ToolchainIdentity(
        identifier=require_truthy_text(
            document.get("identifier"), f"{field} identifier"
        ),
        rustc_vv=require_truthy_text(document.get("rustc_vv"), f"{field} rustc_vv"),
        kani_compiler=require_truthy_text(
            document.get("kani_compiler"), f"{field} kani_compiler"
        ),
        components=_string_array(document.get("components", []), f"{field} components"),
    )


def _parse_solver(document: object) -> SolverRecord:
    """Parse one solver record."""
    mapping = require_mapping(document, "solver")
    reject_unknown_keys(mapping, _SOLVER_KEYS, "solver")
    return SolverRecord(
        name=require_truthy_text(mapping.get("name"), "solver name"),
        version=require_truthy_text(mapping.get("version"), "solver version"),
        sha256=require_sha256(mapping.get("sha256"), "solver sha256"),
    )


def parse_backend(document: dict[str, object], field: str) -> BackendIdentity:
    """Parse the backend identity group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    BackendIdentity
        The validated backend identity and solver inventory.
    """
    reject_unknown_keys(document, _BACKEND_KEYS, field)
    solvers = require_sequence(document.get("solvers", []), f"{field} solvers")
    return BackendIdentity(
        version=require_truthy_text(document.get("version"), f"{field} version"),
        revision=require_full_sha(document.get("revision"), f"{field} revision"),
        solvers=tuple(_parse_solver(item) for item in solvers),
    )


def _optional_text(value: object, field: str) -> str | None:
    """Parse an optional non-empty string."""
    return None if value is None else require_truthy_text(value, field)


def parse_runtime(document: dict[str, object], field: str) -> RuntimeIdentity:
    """Parse the runtime identity group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    RuntimeIdentity
        The validated runtime compatibility record.
    """
    reject_unknown_keys(document, _RUNTIME_KEYS, field)
    return RuntimeIdentity(
        glibc_baseline=require_truthy_text(
            document.get("glibc_baseline"), f"{field} glibc_baseline"
        ),
        cpu_baseline=_optional_text(
            document.get("cpu_baseline"), f"{field} cpu_baseline"
        ),
        dynamic_loader=_optional_text(
            document.get("dynamic_loader"), f"{field} dynamic_loader"
        ),
        shared_libraries=_string_array(
            document.get("shared_libraries", []), f"{field} shared_libraries"
        ),
    )


def _parse_submodule(document: object) -> SubmoduleRecord:
    """Parse one recursive submodule record."""
    mapping = require_mapping(document, "submodule")
    reject_unknown_keys(mapping, _SUBMODULE_KEYS, "submodule")
    return SubmoduleRecord(
        path=require_truthy_text(mapping.get("path"), "submodule path"),
        commit=require_full_sha(mapping.get("commit"), "submodule commit"),
    )


def _parse_lock_digests(value: object) -> tuple[tuple[str, str], ...]:
    """Parse the lockfile digest mapping in sorted key order."""
    mapping = require_mapping(value, "build lock_digests")
    pairs = (
        (path, require_sha256(digest, f"lock digest for {path}"))
        for path, digest in mapping.items()
    )
    return tuple(sorted(pairs))


def parse_build(document: dict[str, object], field: str) -> BuildInputs:
    """Parse the build-input group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    BuildInputs
        The validated build inputs.
    """
    reject_unknown_keys(document, _BUILD_KEYS, field)
    submodules = require_sequence(document.get("submodules"), f"{field} submodules")
    return BuildInputs(
        submodules=tuple(_parse_submodule(item) for item in submodules),
        lock_digests=_parse_lock_digests(document.get("lock_digests")),
        recipe_digest=require_sha256(document.get("recipe_digest"), "recipe_digest"),
    )


def parse_prerequisite(document: object) -> PrerequisiteRecord:
    """Parse one build prerequisite record.

    Parameters
    ----------
    document : object
        Untrusted prerequisite object.

    Returns
    -------
    PrerequisiteRecord
        The validated prerequisite.
    """
    mapping = require_mapping(document, "prerequisite")
    reject_unknown_keys(mapping, _PREREQUISITE_KEYS, "prerequisite")
    return PrerequisiteRecord(
        name=require_truthy_text(mapping.get("name"), "prerequisite name"),
        source=require_truthy_text(mapping.get("source"), "prerequisite source"),
        version=require_truthy_text(mapping.get("version"), "prerequisite version"),
        sha256=require_sha256(mapping.get("sha256"), "prerequisite sha256"),
    )


def parse_cache(document: dict[str, object], field: str) -> CacheObservation:
    """Parse the source-build cache observation.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    CacheObservation
        The validated cache observation.
    """
    reject_unknown_keys(document, _CACHE_KEYS, field)
    return CacheObservation(
        outcome=require_truthy_text(document.get("outcome"), f"{field} outcome"),
        key=_optional_text(document.get("key"), f"{field} key"),
    )


def parse_resources(document: dict[str, object], field: str) -> ResourceObservation:
    """Parse the measured resource observation.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    ResourceObservation
        The validated resource measurements.
    """
    reject_unknown_keys(document, _RESOURCE_KEYS, field)
    return ResourceObservation(
        wall_seconds=require_number(
            document.get("wall_seconds"), f"{field} wall_seconds"
        ),
        disk_high_water_bytes=require_non_negative_int(
            document.get("disk_high_water_bytes"), f"{field} disk_high_water_bytes"
        ),
        asset_bytes=require_non_negative_int(
            document.get("asset_bytes"), f"{field} asset_bytes"
        ),
    )


def _parse_asset(document: object) -> AssetRecord:
    """Parse one published payload record."""
    mapping = require_mapping(document, "asset")
    reject_unknown_keys(mapping, _ASSET_KEYS, "asset")
    role = require_truthy_text(mapping.get("role"), "asset role")
    if role not in ASSET_ROLES:
        msg = f"snapshot asset role must be one of {', '.join(ASSET_ROLES)}"
        raise ProverToolError(msg)
    return AssetRecord(
        role=role,
        name=require_truthy_text(mapping.get("name"), "asset name"),
        media_type=require_truthy_text(mapping.get("media_type"), "asset media_type"),
        size=require_non_negative_int(mapping.get("size"), "asset size"),
        sha256=require_sha256(mapping.get("sha256"), "asset sha256"),
    )


def parse_assets(value: object) -> tuple[AssetRecord, ...]:
    """Parse the asset array and require exactly one asset per role.

    Parameters
    ----------
    value : object
        Untrusted asset array.

    Returns
    -------
    tuple[AssetRecord, ...]
        The validated asset inventory.

    Raises
    ------
    ProverToolError
        Raised when a role is missing or repeated, or when two assets share a
        name.
    """
    items = require_sequence(value, "assets")
    assets = tuple(_parse_asset(item) for item in items)
    roles = [asset.role for asset in assets]
    if sorted(roles) != sorted(ASSET_ROLES):
        msg = (
            f"snapshot manifest must describe exactly one asset per role: {ASSET_ROLES}"
        )
        raise ProverToolError(msg)
    names = [asset.name for asset in assets]
    if len(set(names)) != len(names):
        msg = "snapshot manifest asset names must be unique"
        raise ProverToolError(msg)
    return assets
