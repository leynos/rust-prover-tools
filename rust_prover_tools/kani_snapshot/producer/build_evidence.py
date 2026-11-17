"""Measured build evidence and the assembly input derived from it.

The build job is the only place where the toolchain, backend, lockfiles, cache
and resources are observed. It writes this evidence document, and assembly
re-derives the payload digests itself, so nothing the build job claims about a
payload can reach the manifest. Every identity here is validated with the same
parser the manifest uses, which means an evidence document that would produce
an invalid manifest is refused before a payload is hashed.

The derivation from evidence to assembly input is explicit about what the run
observed and what the pinned recipe already fixes: the job supplies measured
identities and resources, while the recipe supplies the runtime baseline,
prerequisites, submodule revisions and recipe digest. A plan and an evidence
document from different runs cannot be combined.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib
import sys
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import (
    parse_backend,
    parse_cache,
    parse_toolchain,
)
from rust_prover_tools.kani_snapshot.models import (
    SCHEMA_VERSION,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.assemble_input import (
    AssemblyInput,
    assembly_input_to_json,
    resources_document,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildPlan,
    parse_build_plan,
    write_document,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle_model import same_run
from rust_prover_tools.kani_snapshot.producer.recipe import (
    pinned_recipe,
    recipe_digest,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_sequence,
    require_sha256,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "EVIDENCE_SCHEMA_VERSION",
    "BuildEvidence",
    "assembly_input_from_evidence",
    "evidence_to_json",
    "main",
    "parse_build_evidence",
)

#: The only evidence schema version this implementation understands.
EVIDENCE_SCHEMA_VERSION: typ.Final[int] = 1

_EVIDENCE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "toolchain",
    "backend",
    "launcher_package_version",
    "lock_digests",
    "cache",
    "resources",
})
_LOCK_KEYS: typ.Final[frozenset[str]] = frozenset({"path", "sha256"})


@dc.dataclass(frozen=True, slots=True)
class BuildEvidence:
    """Everything the build job measured, except the payload bytes.

    Attributes
    ----------
    schema_version : int
        Evidence schema version. Only version 1 is accepted.
    toolchain : ToolchainIdentity
        Exact compiler identities and recorded toolchain files.
    backend : BackendIdentity
        Upstream backend identity and solver digests.
    launcher_package_version : str
        Version reported by the packaged launcher crate.
    lock_digests : tuple[tuple[str, str], ...]
        Recorded lockfile path and digest pairs, in path order.
    cache : CacheObservation
        Recorded source-build cache outcome.
    wall_seconds : float
        Measured wall-clock build time.
    disk_high_water_bytes : int
        Highest observed disk usage.
    """

    schema_version: int
    toolchain: ToolchainIdentity
    backend: BackendIdentity
    launcher_package_version: str
    lock_digests: tuple[tuple[str, str], ...]
    cache: CacheObservation
    wall_seconds: float
    disk_high_water_bytes: int

    def __post_init__(self) -> None:
        """Reject an evidence document no manifest could be built from."""
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            msg = f"build evidence schema_version must be {EVIDENCE_SCHEMA_VERSION}"
            raise ProverToolError(msg)
        require_truthy_text(
            self.launcher_package_version, "build evidence launcher_package_version"
        )
        paths = [path for path, _ in self.lock_digests]
        if len(set(paths)) != len(paths):
            msg = "build evidence records a lockfile path more than once"
            raise ProverToolError(msg)


def _lock_digests(value: object) -> tuple[tuple[str, str], ...]:
    """Parse the recorded lockfile digests, refusing a repeated path."""
    records: list[tuple[str, str]] = []
    for index, item in enumerate(require_sequence(value, "build evidence locks")):
        field = f"build evidence locks[{index}]"
        document = require_mapping(item, field)
        reject_unknown_keys(document, _LOCK_KEYS, field)
        missing = sorted(_LOCK_KEYS - set(document))
        if missing:
            msg = f"{field} is missing keys: {', '.join(missing)}"
            raise ProverToolError(msg)
        records.append((
            require_truthy_text(document["path"], f"{field} path"),
            require_sha256(document["sha256"], f"{field} sha256"),
        ))
    return tuple(sorted(records))


def parse_build_evidence(data: bytes) -> BuildEvidence:
    """Parse exact evidence bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes written by the build job.

    Returns
    -------
    BuildEvidence
        Validated measurements.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, a wrong schema
        version or an identity the manifest parser would refuse.
    """
    document = require_mapping(
        strict_json_loads(data, "build evidence"), "build evidence"
    )
    reject_unknown_keys(document, _EVIDENCE_KEYS, "build evidence")
    missing = sorted(_EVIDENCE_KEYS - set(document))
    if missing:
        msg = f"build evidence is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    wall_seconds, disk_high_water_bytes = resources_document(document["resources"])
    return BuildEvidence(
        schema_version=require_int(
            document["schema_version"], "build evidence schema_version"
        ),
        toolchain=parse_toolchain(
            require_mapping(document["toolchain"], "build evidence toolchain"),
            "build evidence toolchain",
        ),
        backend=parse_backend(
            require_mapping(document["backend"], "build evidence backend"),
            "build evidence backend",
        ),
        launcher_package_version=require_truthy_text(
            document["launcher_package_version"],
            "build evidence launcher_package_version",
        ),
        lock_digests=_lock_digests(document["lock_digests"]),
        cache=parse_cache(
            require_mapping(document["cache"], "build evidence cache"),
            "build evidence cache",
        ),
        wall_seconds=wall_seconds,
        disk_high_water_bytes=disk_high_water_bytes,
    )


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


def evidence_to_json(evidence: BuildEvidence) -> bytes:
    """Render evidence as deterministic JSON bytes.

    The rendering mirrors `parse_build_evidence`, so the build job can write
    the document assembly later re-reads without a second schema.
    """
    document = {
        "schema_version": evidence.schema_version,
        "toolchain": _toolchain_document(evidence.toolchain),
        "backend": _backend_document(evidence.backend),
        "launcher_package_version": evidence.launcher_package_version,
        "lock_digests": [
            {"path": path, "sha256": digest} for path, digest in evidence.lock_digests
        ],
        "cache": {"outcome": evidence.cache.outcome, "key": evidence.cache.key},
        "resources": {
            "wall_seconds": evidence.wall_seconds,
            "disk_high_water_bytes": evidence.disk_high_water_bytes,
        },
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def assembly_input_from_evidence(
    *,
    plan: BuildPlan,
    workflow: WorkflowIdentity,
    evidence: BuildEvidence,
) -> AssemblyInput:
    """Combine a trusted build plan with the measurements it authorised.

    Parameters
    ----------
    plan : BuildPlan
        Plan written by this run's `build` operation.
    workflow : WorkflowIdentity
        Run that measured the build.
    evidence : BuildEvidence
        Measurements written by the build job.

    Returns
    -------
    AssemblyInput
        Metadata assembly needs, with recipe-owned fields taken from the
        pinned recipe rather than from the job.

    Raises
    ------
    ProverToolError
        Raised when the plan belongs to another run, the evidence names a
        backend revision or launcher version the plan did not authorise, or
        the recipe no longer matches the plan's recorded digest.
    """
    if not same_run(plan.workflow, workflow):
        msg = "build plan belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    recipe = pinned_recipe()
    if evidence.backend.revision != plan.upstream_commit:
        msg = (
            "build evidence backend revision must equal the resolved upstream "
            f"commit {plan.upstream_commit}"
        )
        raise ProverToolError(msg)
    if evidence.launcher_package_version != recipe.upstream.crate_version:
        msg = (
            "build evidence launcher version must equal the pinned launcher "
            f"version {recipe.upstream.crate_version}"
        )
        raise ProverToolError(msg)
    if recipe_digest(recipe) != plan.recipe_digest:
        msg = "build plan was derived from another recipe digest"
        raise ProverToolError(msg)
    return AssemblyInput(
        schema_version=SCHEMA_VERSION,
        tag=plan.tag,
        upstream_commit=plan.upstream_commit,
        builder_commit=plan.builder_commit,
        workflow=workflow,
        toolchain=evidence.toolchain,
        launcher_package_version=evidence.launcher_package_version,
        backend=evidence.backend,
        runtime=recipe.runtime,
        build=BuildInputs(
            submodules=recipe.submodules,
            lock_digests=evidence.lock_digests,
            recipe_digest=plan.recipe_digest,
        ),
        prerequisites=recipe.prerequisites,
        cache=evidence.cache,
        wall_seconds=evidence.wall_seconds,
        disk_high_water_bytes=evidence.disk_high_water_bytes,
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the build-evidence command line parser."""
    parser = argparse.ArgumentParser(
        prog="kani-snapshot-build-evidence",
        description="Derive an assembly input from measured build evidence.",
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument(
        "--event", choices=("schedule", "workflow_dispatch"), required=True
    )
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    return parser


def _workflow(namespace: argparse.Namespace) -> WorkflowIdentity:
    """Build the trusted workflow identity from parsed arguments."""
    return WorkflowIdentity(
        path=WORKFLOW_PATH,
        event=require_truthy_text(namespace.event, "workflow event"),
        ref=WORKFLOW_REF,
        run_id=require_int(namespace.run_id, "workflow run_id"),
        run_attempt=require_int(namespace.run_attempt, "workflow run_attempt"),
        run_url=namespace.run_url,
    )


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write the assembly input for one trusted run."""
    namespace = build_parser().parse_args(argv)
    try:
        plan = parse_build_plan(pathlib.Path(namespace.plan).read_bytes())
        evidence = parse_build_evidence(pathlib.Path(namespace.evidence).read_bytes())
        assembly = assembly_input_from_evidence(
            plan=plan,
            workflow=_workflow(namespace),
            evidence=evidence,
        )
        write_document(pathlib.Path(namespace.output), assembly_input_to_json(assembly))
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-build-evidence: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
