"""Derive the assembly input from measured build evidence.

The evidence document itself lives in `evidence_model`; this module owns the
derivation that combines it with a trusted build plan. The derivation is
explicit about what the run observed and what the pinned recipe already fixes:
the job supplies measured identities and resources, while the recipe supplies
the runtime baseline, prerequisites, submodule revisions and recipe digest. A
plan and an evidence document from different runs cannot be combined.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import (
    SCHEMA_VERSION,
    BuildInputs,
    SubmoduleRecord,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.orchestrator.documents import (
    write_document,
)
from rust_prover_tools.kani_snapshot.producer.assemble_input import (
    AssemblyInput,
    assembly_input_to_json,
)
from rust_prover_tools.kani_snapshot.producer.evidence_model import (
    EVIDENCE_SCHEMA_VERSION,
    BuildEvidence,
    evidence_to_json,
    parse_build_evidence,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildPlan,
    parse_build_plan,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle_model import same_run
from rust_prover_tools.kani_snapshot.producer.recipe import (
    pinned_recipe,
    recipe_digest,
)
from rust_prover_tools.kani_snapshot.validation import require_int

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.producer.recipe_model import Recipe

__all__ = (
    "EVIDENCE_SCHEMA_VERSION",
    "BuildEvidence",
    "assembly_input_from_evidence",
    "evidence_to_json",
    "main",
    "parse_build_evidence",
)


def _require_submodules(
    observed: tuple[SubmoduleRecord, ...],
    recorded: tuple[SubmoduleRecord, ...],
) -> tuple[SubmoduleRecord, ...]:
    """Require the checkout's recursive submodules to equal the recipe's.

    The recipe's gitlinks are reviewed data. The build job measures the
    checkout, and the two must agree path for path and commit for commit, so
    an evidence document cannot substitute one tree for the recipe's.
    """
    wanted = {(record.path, record.commit) for record in recorded}
    seen = {(record.path, record.commit) for record in observed}
    if seen == wanted:
        return observed
    missing = sorted(f"{path}@{commit}" for path, commit in wanted - seen)
    extra = sorted(f"{path}@{commit}" for path, commit in seen - wanted)
    msg = (
        "build evidence submodules do not match the pinned recipe"
        f"; missing: {', '.join(missing) or 'none'}"
        f"; unexpected: {', '.join(extra) or 'none'}"
    )
    raise ProverToolError(msg)


def _require_consistent(
    *,
    plan: BuildPlan,
    workflow: WorkflowIdentity,
    evidence: BuildEvidence,
    recipe: Recipe,
) -> None:
    """Require the plan, evidence and pinned recipe to describe one build."""
    checks = (
        (
            not same_run(plan.workflow, workflow),
            "build plan belongs to another workflow run or attempt",
        ),
        (
            evidence.backend.revision != plan.upstream_commit,
            (
                "build evidence backend revision must equal the resolved "
                f"upstream commit {plan.upstream_commit}"
            ),
        ),
        (
            evidence.launcher_package_version != recipe.upstream.crate_version,
            (
                "build evidence launcher version must equal the pinned "
                f"launcher version {recipe.upstream.crate_version}"
            ),
        ),
        (
            recipe_digest(recipe) != plan.recipe_digest,
            "build plan was derived from another recipe digest",
        ),
    )
    for failed, message in checks:
        if failed:
            raise ProverToolError(message)


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
    recipe = pinned_recipe()
    _require_consistent(
        plan=plan,
        workflow=workflow,
        evidence=evidence,
        recipe=recipe,
    )
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
            submodules=_require_submodules(evidence.submodules, recipe.submodules),
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
        event=typ.cast("str", namespace.event),
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
