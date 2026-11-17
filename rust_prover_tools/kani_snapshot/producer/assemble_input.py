"""Assembled metadata handed from the build job to assembly.

The input document carries identities and measurements but never asset digests:
`assemble` re-reads and hashes every payload itself, so a build job cannot
smuggle a claim into the manifest. These records validate everything that can be
checked without the payload bytes, and the parser mirrors the renderer exactly,
so an unknown or missing field fails closed.
"""

from __future__ import annotations

import dataclasses as dc
import json
import math
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import (
    parse_backend,
    parse_build,
    parse_cache,
    parse_prerequisite,
    parse_runtime,
    parse_toolchain,
    parse_workflow,
)
from rust_prover_tools.kani_snapshot.models import (
    SCHEMA_VERSION,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    PrerequisiteRecord,
    RuntimeIdentity,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.lifecycle_model import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_non_negative_int,
    require_sequence,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "AssemblyInput",
    "AssemblyRequest",
    "assembly_input_to_json",
    "parse_assembly_input",
    "resources_document",
)

_INPUT_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "tag",
    "upstream_commit",
    "builder_commit",
    "workflow",
    "toolchain",
    "launcher_package_version",
    "backend",
    "runtime",
    "build",
    "prerequisites",
    "cache",
    "resources",
})
_RESOURCE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "wall_seconds",
    "disk_high_water_bytes",
})


@dc.dataclass(frozen=True, slots=True)
class AssemblyInput:
    """Everything assembly needs except the payload bytes themselves.

    Attributes
    ----------
    schema_version : int
        Input schema version. Only version 1 is accepted.
    tag : str
        Immutable snapshot tag.
    upstream_commit : str
        Full upstream commit.
    builder_commit : str
        Full builder commit.
    workflow : WorkflowIdentity
        Run that measured the build.
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
    wall_seconds : float
        Measured wall-clock build time.
    disk_high_water_bytes : int
        Highest observed disk usage.
    """

    schema_version: int
    tag: str
    upstream_commit: str
    builder_commit: str
    workflow: WorkflowIdentity
    toolchain: ToolchainIdentity
    launcher_package_version: str
    backend: BackendIdentity
    runtime: RuntimeIdentity
    build: BuildInputs
    prerequisites: tuple[PrerequisiteRecord, ...]
    cache: CacheObservation
    wall_seconds: float
    disk_high_water_bytes: int

    def __post_init__(self) -> None:
        """Reject an input that could not describe a valid manifest."""
        if self.schema_version != SCHEMA_VERSION:
            msg = f"assembly schema_version must be {SCHEMA_VERSION}"
            raise ProverToolError(msg)
        validate_tag(self.tag, self.upstream_commit)
        require_full_sha(self.upstream_commit, "assembly upstream_commit")
        require_full_sha(self.builder_commit, "assembly builder_commit")
        require_truthy_text(
            self.launcher_package_version, "assembly launcher_package_version"
        )
        _require_trusted_workflow(self.workflow)
        _require_wall_seconds(self.wall_seconds)
        require_non_negative_int(
            self.disk_high_water_bytes, "assembly disk_high_water_bytes"
        )


@dc.dataclass(frozen=True, slots=True)
class AssemblyRequest:
    """One assembly attempt: measured metadata and a payload directory.

    Attributes
    ----------
    payloads : Path
        Directory holding exactly the four payload files.
    inputs : AssemblyInput
        Measured metadata from the build job.
    workflow : WorkflowIdentity
        Run that is assembling the manifest.
    """

    payloads: Path
    inputs: AssemblyInput
    workflow: WorkflowIdentity


def _require_trusted_workflow(workflow: WorkflowIdentity) -> None:
    """Require the producer workflow identity this repository trusts."""
    if workflow.path != WORKFLOW_PATH:
        msg = f"assembly workflow path must be {WORKFLOW_PATH}"
        raise ProverToolError(msg)
    if workflow.ref != WORKFLOW_REF:
        msg = f"assembly workflow ref must be {WORKFLOW_REF}"
        raise ProverToolError(msg)


def _require_wall_seconds(value: object) -> float:
    """Require a finite, non-negative wall-clock measurement."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = "assembly wall_seconds must be a number"
        raise ProverToolError(msg)
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        msg = "assembly wall_seconds must be finite and non-negative"
        raise ProverToolError(msg)
    return seconds


def resources_document(value: object) -> tuple[float, int]:
    """Parse the two measured resource fields assembly consumes."""
    document = require_mapping(value, "assembly resources")
    reject_unknown_keys(document, _RESOURCE_KEYS, "assembly resources")
    missing = sorted(_RESOURCE_KEYS - set(document))
    if missing:
        msg = f"assembly resources is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    return (
        _require_wall_seconds(document["wall_seconds"]),
        require_non_negative_int(
            document["disk_high_water_bytes"], "assembly disk_high_water_bytes"
        ),
    )


def parse_assembly_input(data: bytes) -> AssemblyInput:
    """Parse exact assembly-input bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes as written by this module.

    Returns
    -------
    AssemblyInput
        Validated metadata for assembly.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, wrong types or an
        identity that cannot produce a valid manifest.
    """
    document = require_mapping(strict_json_loads(data, "assembly input"), "assembly")
    reject_unknown_keys(document, _INPUT_KEYS, "assembly input")
    missing = sorted(_INPUT_KEYS - set(document))
    if missing:
        msg = f"assembly input is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    wall_seconds, disk_high_water_bytes = resources_document(document["resources"])
    return AssemblyInput(
        schema_version=require_int(
            document["schema_version"], "assembly schema_version"
        ),
        tag=require_truthy_text(document["tag"], "assembly tag"),
        upstream_commit=require_full_sha(
            document["upstream_commit"], "assembly upstream_commit"
        ),
        builder_commit=require_full_sha(
            document["builder_commit"], "assembly builder_commit"
        ),
        workflow=parse_workflow(
            require_mapping(document["workflow"], "assembly workflow"),
            "assembly workflow",
        ),
        toolchain=parse_toolchain(
            require_mapping(document["toolchain"], "assembly toolchain"),
            "assembly toolchain",
        ),
        launcher_package_version=require_truthy_text(
            document["launcher_package_version"],
            "assembly launcher_package_version",
        ),
        backend=parse_backend(
            require_mapping(document["backend"], "assembly backend"),
            "assembly backend",
        ),
        runtime=parse_runtime(
            require_mapping(document["runtime"], "assembly runtime"),
            "assembly runtime",
        ),
        build=parse_build(
            require_mapping(document["build"], "assembly build"), "assembly build"
        ),
        prerequisites=tuple(
            parse_prerequisite(item)
            for item in require_sequence(
                document["prerequisites"], "assembly prerequisites"
            )
        ),
        cache=parse_cache(
            require_mapping(document["cache"], "assembly cache"), "assembly cache"
        ),
        wall_seconds=wall_seconds,
        disk_high_water_bytes=disk_high_water_bytes,
    )


def assembly_input_to_json(inputs: AssemblyInput) -> bytes:
    """Render assembly metadata as deterministic JSON bytes.

    The rendering mirrors the strict parser, so a build job can write the
    document that assembly later re-reads without a second schema.
    """
    document = {
        "schema_version": inputs.schema_version,
        "tag": inputs.tag,
        "upstream_commit": inputs.upstream_commit,
        "builder_commit": inputs.builder_commit,
        "workflow": {
            "path": inputs.workflow.path,
            "event": inputs.workflow.event,
            "ref": inputs.workflow.ref,
            "run_id": inputs.workflow.run_id,
            "run_attempt": inputs.workflow.run_attempt,
            "run_url": inputs.workflow.run_url,
        },
        "toolchain": _toolchain_document(inputs.toolchain),
        "launcher_package_version": inputs.launcher_package_version,
        "backend": _backend_document(inputs.backend),
        "runtime": _runtime_document(inputs.runtime),
        "build": _build_document(inputs.build),
        "prerequisites": [
            {
                "name": item.name,
                "source": item.source,
                "version": item.version,
                "sha256": item.sha256,
            }
            for item in inputs.prerequisites
        ],
        "cache": {"outcome": inputs.cache.outcome, "key": inputs.cache.key},
        "resources": {
            "wall_seconds": inputs.wall_seconds,
            "disk_high_water_bytes": inputs.disk_high_water_bytes,
        },
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def _toolchain_document(toolchain: ToolchainIdentity) -> dict[str, object]:
    """Render the toolchain group in the manifest parser's shape."""
    return {
        "identifier": toolchain.identifier,
        "rustc_vv": toolchain.rustc_vv,
        "kani_compiler": toolchain.kani_compiler,
        "channel_metadata_sha256": toolchain.channel_metadata_sha256,
        "components": [
            {
                "name": component.name,
                "version": component.version,
                "url": component.url,
                "size": component.size,
                "sha256": component.sha256,
            }
            for component in toolchain.components
        ],
        "rustc_sha256": toolchain.rustc_sha256,
        "rustup_components_sha256": toolchain.rustup_components_sha256,
        "libraries": [
            {"path": library.path, "sha256": library.sha256}
            for library in toolchain.libraries
        ],
    }


def _backend_document(backend: BackendIdentity) -> dict[str, object]:
    """Render the backend group in the manifest parser's shape."""
    return {
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
    }


def _runtime_document(runtime: RuntimeIdentity) -> dict[str, object]:
    """Render the runtime group in the manifest parser's shape."""
    return {
        "glibc_baseline": runtime.glibc_baseline,
        "cpu_baseline": runtime.cpu_baseline,
        "dynamic_loader": runtime.dynamic_loader,
        "shared_libraries": list(runtime.shared_libraries),
    }


def _build_document(build: BuildInputs) -> dict[str, object]:
    """Render the build-input group in the manifest parser's shape."""
    return {
        "submodules": [
            {"path": item.path, "commit": item.commit} for item in build.submodules
        ],
        "lock_digests": dict(build.lock_digests),
        "recipe_digest": build.recipe_digest,
    }
