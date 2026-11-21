"""Typed records handed between the guarded producer jobs.

Each workflow job writes one canonical JSON document that the next job strictly
parses. The binding rule is the reason these records exist: a resolution or a
build plan is only usable by the workflow run and attempt that produced it, so
a stale artefact left by an earlier job cannot authorise a later effect. The
canonical rendering and parsing live in `lifecycle`; this module holds the
shapes those documents must satisfy.

`Resolution` records which upstream commit a run may build and how it was
chosen. `BuildPlan` records the exact argument vectors derived from the pinned
recipe. Both carry the trusted workflow identity, and every constructor refuses
an identity that does not name this workflow on `main`, because a plan from a
pull-request run must never reach a publishing job.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_positive_int,
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
    "same_run",
)

#: The only schema version these records accept.
LIFECYCLE_SCHEMA_VERSION: typ.Final[int] = 1

#: The workflow whose run identity every record must name.
WORKFLOW_PATH: typ.Final[str] = ".github/workflows/kani-monthly.yml"

#: Producer runs are only trusted from the default branch.
WORKFLOW_REF: typ.Final[str] = "refs/heads/main"


class ResolutionRoute(enum.Enum):
    """How this run chose its upstream commit.

    Attributes
    ----------
    MANUAL : str
        A `workflow_dispatch` input supplied a full commit.
    SCHEDULED : str
        The newest qualified upstream release was selected.
    """

    MANUAL = "manual"
    SCHEDULED = "scheduled"


@dc.dataclass(frozen=True, slots=True)
class Resolution:
    """The upstream commit a run is allowed to build.

    Attributes
    ----------
    schema_version : int
        Record schema version. Only version 1 is accepted.
    route : ResolutionRoute
        How the commit was chosen.
    upstream_commit : str
        Full 40-hex `model-checking/kani` commit.
    upstream_tag : str
        Upstream release tag naming that commit.
    workflow : WorkflowIdentity
        Trusted run that produced this resolution.
    """

    schema_version: int
    route: ResolutionRoute
    upstream_commit: str
    upstream_tag: str
    workflow: WorkflowIdentity

    def __post_init__(self) -> None:
        """Reject a resolution that cannot authorise a build."""
        _require_schema(self.schema_version, "resolution")
        require_full_sha(self.upstream_commit, "resolution upstream_commit")
        require_truthy_text(self.upstream_tag, "resolution upstream_tag")
        _require_workflow(self.workflow, self.route)


@dc.dataclass(frozen=True, slots=True)
class BuildPlan:
    """The exact build work one trusted run may perform.

    Attributes
    ----------
    schema_version : int
        Record schema version. Only version 1 is accepted.
    tag : str
        Immutable snapshot tag this plan publishes under.
    upstream_commit : str
        Full upstream commit from the bound resolution.
    builder_commit : str
        Full commit of this repository that runs the build.
    target : str
        The single supported target triple.
    recipe_digest : str
        Digest of the pinned recipe the plan was derived from.
    bundle_asset : str
        Upstream bundle asset name.
    launcher_asset : str
        Launcher archive name this project publishes.
    commands : tuple[tuple[str, ...], ...]
        Argument vectors, in execution order. Never shell strings.
    workflow : WorkflowIdentity
        Trusted run that may execute the plan.
    """

    schema_version: int
    tag: str
    upstream_commit: str
    builder_commit: str
    target: str
    recipe_digest: str
    bundle_asset: str
    launcher_asset: str
    commands: tuple[tuple[str, ...], ...]
    workflow: WorkflowIdentity

    def __post_init__(self) -> None:
        """Reject a plan whose identity or commands are not trusted."""
        _require_schema(self.schema_version, "build plan")
        validate_tag(self.tag, self.upstream_commit)
        require_full_sha(self.upstream_commit, "build plan upstream_commit")
        require_full_sha(self.builder_commit, "build plan builder_commit")
        validate_target(self.target)
        require_sha256(self.recipe_digest, "build plan recipe_digest")
        _require_commands(self.commands)


@dc.dataclass(frozen=True, slots=True)
class BuildRequest:
    """Everything needed to derive one build plan.

    Attributes
    ----------
    resolution : Resolution
        Upstream identity selected for this run.
    workflow : WorkflowIdentity
        Run asking for the plan.
    builder_commit : str
        Full commit of this repository that will run the build.
    tag : str
        Immutable publication identity reserved for this plan.
    """

    resolution: Resolution
    workflow: WorkflowIdentity
    builder_commit: str
    tag: str


def _require_schema(value: int, field: str) -> int:
    """Require the single supported lifecycle schema version."""
    version = require_int(value, f"{field} schema_version")
    if version != LIFECYCLE_SCHEMA_VERSION:
        msg = f"{field} schema_version must be {LIFECYCLE_SCHEMA_VERSION}"
        raise ProverToolError(msg)
    return version


def _require_workflow(workflow: WorkflowIdentity, route: ResolutionRoute) -> None:
    """Require a producer identity this repository trusts to publish."""
    if workflow.path != WORKFLOW_PATH:
        msg = f"producer workflow path must be {WORKFLOW_PATH}"
        raise ProverToolError(msg)
    if workflow.ref != WORKFLOW_REF:
        msg = f"producer workflow ref must be {WORKFLOW_REF}"
        raise ProverToolError(msg)
    expected = "schedule" if route is ResolutionRoute.SCHEDULED else "workflow_dispatch"
    if workflow.event != expected:
        msg = f"{route.value} resolution must come from a {expected} run"
        raise ProverToolError(msg)
    require_positive_int(workflow.run_id, "producer run_id")
    require_positive_int(workflow.run_attempt, "producer run_attempt")


def _require_commands(commands: tuple[tuple[str, ...], ...]) -> None:
    """Require argument vectors that can be executed without a shell."""
    if not commands:
        msg = "build plan must record at least one command"
        raise ProverToolError(msg)
    for command in commands:
        if not command or any(not item for item in command):
            msg = "build plan commands must be non-empty argument vectors"
            raise ProverToolError(msg)


def same_run(left: WorkflowIdentity, right: WorkflowIdentity) -> bool:
    """Return whether two records come from the same workflow attempt."""
    return (
        left.path == right.path
        and left.run_id == right.run_id
        and left.run_attempt == right.run_attempt
    )
