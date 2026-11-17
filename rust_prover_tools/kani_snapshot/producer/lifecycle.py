"""Canonical documents and plan derivation for the guarded producer jobs.

`lifecycle_model` holds the records; this module derives a build plan from the
pinned recipe and renders or strictly parses the canonical JSON a workflow job
hands to the next one. Rendering and parsing are written against the same key
sets, so a document written here can only be read back by this producer, and an
unknown or missing field fails closed rather than being guessed at.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import parse_workflow
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.commands import (
    bundle_asset_name,
    bundle_command,
    launcher_asset_name,
    launcher_package_command,
    toolchain_install_command,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle_model import (
    LIFECYCLE_SCHEMA_VERSION,
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildPlan,
    BuildRequest,
    Resolution,
    ResolutionRoute,
    same_run,
)
from rust_prover_tools.kani_snapshot.producer.recipe import (
    pinned_recipe,
    recipe_digest,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sequence,
    require_sha256,
    require_truthy_text,
    validate_target,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = (
    "LIFECYCLE_SCHEMA_VERSION",
    "WORKFLOW_PATH",
    "WORKFLOW_REF",
    "BuildPlan",
    "BuildRequest",
    "Resolution",
    "ResolutionRoute",
    "parse_build_plan",
    "parse_resolution",
    "plan_build",
    "plan_to_json",
    "resolution_to_json",
    "same_run",
)

_RESOLUTION_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "route",
    "upstream_commit",
    "upstream_tag",
    "workflow",
})
_PLAN_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "tag",
    "upstream_commit",
    "builder_commit",
    "target",
    "recipe_digest",
    "bundle_asset",
    "launcher_asset",
    "commands",
    "workflow",
})


def plan_build(request: BuildRequest) -> BuildPlan:
    """Derive the pinned build plan for one trusted run.

    Parameters
    ----------
    request : BuildRequest
        Resolution, run identity, builder commit and publication tag.

    Returns
    -------
    BuildPlan
        Plan whose commands are the pinned recipe's argument vectors.

    Raises
    ------
    ProverToolError
        Raised when the resolution belongs to another run, names an upstream
        commit other than the pinned recipe, or carries an invalid identity.
    """
    if not same_run(request.resolution.workflow, request.workflow):
        msg = "build plan request does not belong to the resolving run"
        raise ProverToolError(msg)
    recipe = pinned_recipe()
    if request.resolution.upstream_commit != recipe.upstream.commit:
        msg = (
            "build plan may only use the pinned upstream commit "
            f"{recipe.upstream.commit}"
        )
        raise ProverToolError(msg)
    if request.resolution.upstream_tag != recipe.upstream.tag:
        msg = f"build plan may only use the pinned upstream tag {recipe.upstream.tag}"
        raise ProverToolError(msg)
    validate_tag(request.tag, request.resolution.upstream_commit)
    return BuildPlan(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        tag=request.tag,
        upstream_commit=request.resolution.upstream_commit,
        builder_commit=require_full_sha(
            request.builder_commit, "build plan builder_commit"
        ),
        target=recipe.target,
        recipe_digest=recipe_digest(recipe),
        bundle_asset=bundle_asset_name(recipe.upstream.crate_version, recipe.target),
        launcher_asset=launcher_asset_name(
            recipe.upstream.crate_version, recipe.target
        ),
        commands=(
            toolchain_install_command(recipe.upstream.toolchain_channel),
            bundle_command(recipe.upstream.crate_version),
            launcher_package_command(recipe.launcher.package),
        ),
        workflow=request.workflow,
    )


def _workflow_document(workflow: WorkflowIdentity) -> dict[str, object]:
    """Render a workflow identity as JSON-compatible values."""
    return {
        "path": workflow.path,
        "event": workflow.event,
        "ref": workflow.ref,
        "run_id": workflow.run_id,
        "run_attempt": workflow.run_attempt,
        "run_url": workflow.run_url,
    }


def _encode(document: dict[str, object]) -> bytes:
    """Render a document as canonical UTF-8 JSON with a final newline."""
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def resolution_to_json(resolution: Resolution) -> bytes:
    """Render a resolution as deterministic JSON bytes."""
    return _encode({
        "schema_version": resolution.schema_version,
        "route": resolution.route.value,
        "upstream_commit": resolution.upstream_commit,
        "upstream_tag": resolution.upstream_tag,
        "workflow": _workflow_document(resolution.workflow),
    })


def plan_to_json(plan: BuildPlan) -> bytes:
    """Render a build plan as deterministic JSON bytes."""
    return _encode({
        "schema_version": plan.schema_version,
        "tag": plan.tag,
        "upstream_commit": plan.upstream_commit,
        "builder_commit": plan.builder_commit,
        "target": plan.target,
        "recipe_digest": plan.recipe_digest,
        "bundle_asset": plan.bundle_asset,
        "launcher_asset": plan.launcher_asset,
        "commands": [list(command) for command in plan.commands],
        "workflow": _workflow_document(plan.workflow),
    })


def _document(data: bytes, allowed: frozenset[str], field: str) -> dict[str, object]:
    """Parse one strict document and require its exact key set."""
    document = require_mapping(strict_json_loads(data, field), field)
    reject_unknown_keys(document, allowed, field)
    missing = sorted(allowed - set(document))
    if missing:
        msg = f"{field} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    return document


def _parse_route(value: object) -> ResolutionRoute:
    """Parse a route name, refusing anything the producer does not run."""
    text = require_truthy_text(value, "resolution route")
    try:
        return ResolutionRoute(text)
    except ValueError as exc:
        msg = f"resolution route must be manual or scheduled, got {text!r}"
        raise ProverToolError(msg) from exc


def _parse_workflow(value: object, field: str) -> WorkflowIdentity:
    """Parse the workflow group of a lifecycle document."""
    return parse_workflow(require_mapping(value, field), field)


def parse_resolution(data: bytes) -> Resolution:
    """Parse exact resolution bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes as written by this module.

    Returns
    -------
    Resolution
        Validated resolution.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, or an identity this
        repository does not trust.
    """
    document = _document(data, _RESOLUTION_KEYS, "resolution")
    return Resolution(
        schema_version=require_int(
            document["schema_version"], "resolution schema_version"
        ),
        route=_parse_route(document["route"]),
        upstream_commit=require_full_sha(
            document["upstream_commit"], "resolution upstream_commit"
        ),
        upstream_tag=require_truthy_text(
            document["upstream_tag"], "resolution upstream_tag"
        ),
        workflow=_parse_workflow(document["workflow"], "resolution workflow"),
    )


def _parse_commands(value: object) -> tuple[tuple[str, ...], ...]:
    """Parse a list of argument vectors, refusing a shell string."""
    commands: list[tuple[str, ...]] = []
    for index, item in enumerate(require_sequence(value, "build plan commands")):
        members = require_sequence(item, f"build plan command {index}")
        commands.append(
            tuple(
                require_truthy_text(member, f"build plan command {index} argument")
                for member in members
            )
        )
    return tuple(commands)


def parse_build_plan(data: bytes) -> BuildPlan:
    """Parse exact build-plan bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes as written by this module.

    Returns
    -------
    BuildPlan
        Validated plan.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, an identity that is
        not trusted, or commands that are not argument vectors.
    """
    document = _document(data, _PLAN_KEYS, "build plan")
    return BuildPlan(
        schema_version=require_int(
            document["schema_version"], "build plan schema_version"
        ),
        tag=require_truthy_text(document["tag"], "build plan tag"),
        upstream_commit=require_full_sha(
            document["upstream_commit"], "build plan upstream_commit"
        ),
        builder_commit=require_full_sha(
            document["builder_commit"], "build plan builder_commit"
        ),
        target=validate_target(document["target"]),
        recipe_digest=require_sha256(
            document["recipe_digest"], "build plan recipe_digest"
        ),
        bundle_asset=require_truthy_text(
            document["bundle_asset"], "build plan bundle_asset"
        ),
        launcher_asset=require_truthy_text(
            document["launcher_asset"], "build plan launcher_asset"
        ),
        commands=_parse_commands(document["commands"]),
        workflow=_parse_workflow(document["workflow"], "build plan workflow"),
    )
