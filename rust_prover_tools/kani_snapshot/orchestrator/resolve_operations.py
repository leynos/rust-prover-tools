"""The resolve job: choose the upstream commit and record the resolution.

`entrypoint` owns the command line and `operations` owns the remaining jobs;
this module owns the one operation that turns a dispatch input or a collected
candidate document into a same-run resolution. It also owns the trusted
workflow identity every producer job starts from, because resolution is where
that identity is first bound to a document.
"""

from __future__ import annotations

import pathlib
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import WorkflowIdentity
from rust_prover_tools.kani_snapshot.orchestrator.documents import (
    write_document,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
    Resolution,
    ResolutionRoute,
    resolution_to_json,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.resolution import (
    CandidateCommit,
    candidate_from_api,
    parse_commit_checks,
    resolve_manual_commit,
    select_newest_candidate,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_non_negative_int,
    require_sequence,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import argparse

__all__ = (
    "TRUSTED_EVENTS",
    "resolve",
    "workflow_identity",
)

_CANDIDATE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "release",
    "tag_ref",
    "total_count",
    "checks",
})
#: Events whose identity the producer trusts.
TRUSTED_EVENTS: typ.Final[tuple[str, ...]] = (
    "schedule",
    "workflow_dispatch",
)


def workflow_identity(namespace: argparse.Namespace) -> WorkflowIdentity:
    """Build the trusted workflow identity from parsed arguments."""
    return WorkflowIdentity(
        path=WORKFLOW_PATH,
        event=require_truthy_text(namespace.event, "workflow event"),
        ref=WORKFLOW_REF,
        run_id=require_int(namespace.run_id, "workflow run_id"),
        run_attempt=require_int(namespace.run_attempt, "workflow run_attempt"),
        run_url=namespace.run_url,
    )


def _candidate(index: int, record: dict[str, object]) -> CandidateCommit:
    """Parse one candidate, refusing an incomplete check observation."""
    checks = require_sequence(record["checks"], f"candidate {index} checks")
    total = require_non_negative_int(
        record["total_count"], f"candidate {index} total_count"
    )
    if total != len(checks):
        msg = (
            f"candidate {index} records {len(checks)} of {total} check runs; a "
            "truncated observation cannot prove the release qualified"
        )
        raise ProverToolError(msg)
    return candidate_from_api(
        require_mapping(record["release"], f"candidate {index} release"),
        require_mapping(record["tag_ref"], f"candidate {index} tag_ref"),
        checks,
    )


def _candidates(data: bytes) -> list[CandidateCommit]:
    """Parse the candidate document written by the resolve job's collector."""
    items = require_sequence(
        strict_json_loads(data, "candidate document"), "candidates"
    )
    parsed: list[CandidateCommit] = []
    for index, item in enumerate(items):
        record = require_mapping(item, f"candidate {index}")
        reject_unknown_keys(record, _CANDIDATE_KEYS, f"candidate {index}")
        missing = sorted(_CANDIDATE_KEYS - set(record))
        if missing:
            msg = f"candidate {index} is missing keys: {', '.join(missing)}"
            raise ProverToolError(msg)
        parsed.append(_candidate(index, record))
    return parsed


def _require_manual_inputs(namespace: argparse.Namespace) -> None:
    """Require the commit and its observed check evidence for a dispatch."""
    if not namespace.commit:
        msg = "manual resolution requires --commit"
        raise ProverToolError(msg)
    if not namespace.checks:
        msg = (
            "manual resolution requires --checks: a dispatched commit is only "
            "buildable with observed evidence that its required upstream "
            "checks passed"
        )
        raise ProverToolError(msg)


def _manual_resolution(
    namespace: argparse.Namespace,
    workflow: WorkflowIdentity,
) -> Resolution:
    """Build a resolution from an explicit full commit.

    The upstream tag comes from the pinned recipe unless the caller names one.
    A dispatch input can only name the commit it intends to build; letting it
    choose the tag would allow one release's identity to be paired with another
    release's source, and the plan refuses that pairing later anyway.

    The commit is accepted only when the collected check evidence for it shows
    that every required upstream check passed, so a dispatch cannot bypass the
    qualification a scheduled candidate must meet.
    """
    commit = resolve_manual_commit(namespace.commit)
    checked = parse_commit_checks(pathlib.Path(namespace.checks).read_bytes())
    if checked != commit:
        msg = (
            f"manual check evidence is about {checked}, not the dispatched "
            f"commit {commit}"
        )
        raise ProverToolError(msg)
    tag = namespace.upstream_tag or pinned_recipe().upstream.tag
    return Resolution(
        schema_version=1,
        route=ResolutionRoute.MANUAL,
        upstream_commit=commit,
        upstream_tag=require_truthy_text(tag, "upstream tag"),
        workflow=workflow,
    )


def _scheduled_resolution(
    namespace: argparse.Namespace,
    workflow: WorkflowIdentity,
) -> Resolution:
    """Select the newest qualified release, if it is the reviewed one.

    The build plan may only use the pinned recipe's commit, because the recipe
    is reviewed data: a new upstream release is built after a reviewed recipe
    update, not by drifting to whatever upstream published most recently.
    Selection therefore still observes upstream, but a newer qualified release
    is refused with the action it needs rather than silently building the
    pinned commit while recording the new tag.
    """
    candidates = _candidates(pathlib.Path(namespace.candidates).read_bytes())
    commit = select_newest_candidate(candidates)
    selected = next(candidate for candidate in candidates if candidate.commit == commit)
    recipe = pinned_recipe()
    if (commit, selected.tag) != (recipe.upstream.commit, recipe.upstream.tag):
        msg = (
            f"the newest qualified upstream release {selected.tag} ({commit}) is "
            f"not the pinned recipe {recipe.upstream.tag} "
            f"({recipe.upstream.commit}); update the recipe before building it"
        )
        raise ProverToolError(msg)
    return Resolution(
        schema_version=1,
        route=ResolutionRoute.SCHEDULED,
        upstream_commit=commit,
        upstream_tag=selected.tag,
        workflow=workflow,
    )


def resolve(namespace: argparse.Namespace) -> int:
    """Select the upstream commit this run may build."""
    workflow = workflow_identity(namespace)
    if namespace.route == ResolutionRoute.MANUAL.value:
        _require_manual_inputs(namespace)
        resolution = _manual_resolution(namespace, workflow)
    else:
        if not namespace.candidates:
            msg = "scheduled resolution requires --candidates"
            raise ProverToolError(msg)
        resolution = _scheduled_resolution(namespace, workflow)
    write_document(pathlib.Path(namespace.output), resolution_to_json(resolution))
    return 0
