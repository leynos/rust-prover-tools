"""The whole effect of each producer job, as a callable operation.

`entrypoint` owns the command line; this module owns what a job does once its
arguments are parsed. Each operation reads the typed documents and files the
earlier jobs produced, checks their identity and same-run binding, and only
then writes its own output. Keeping the effects here leaves the command line a
thin, auditable dispatch table.
"""

from __future__ import annotations

import pathlib
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest import ParsedManifest, parse_manifest
from rust_prover_tools.kani_snapshot.models import REPOSITORY, WorkflowIdentity
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.assemble import (
    MANIFEST_NAME,
    AssemblyRequest,
    parse_assembly_input,
)
from rust_prover_tools.kani_snapshot.producer.assemble import (
    assemble as assemble_release,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildRequest,
    Resolution,
    ResolutionRoute,
    parse_resolution,
    plan_build,
    plan_to_json,
    resolution_to_json,
    same_run,
    write_document,
)
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    PUBLICATION_IDENTITY_SCHEMA_VERSION,
    PublicationIdentity,
    allocate_publication_id,
    parse_publication_identity,
    parse_publications,
    publication_identity_to_json,
    publication_month,
)
from rust_prover_tools.kani_snapshot.producer.release_assets import (
    CHECKSUMS_NAME,
    release_assets,
    verify_release,
)
from rust_prover_tools.kani_snapshot.producer.resolution import (
    CandidateCommit,
    candidate_from_api,
    resolve_manual_commit,
    select_newest_candidate,
)
from rust_prover_tools.kani_snapshot.producer.validation_receipt import (
    build_receipt,
    parse_receipt,
    receipt_to_json,
    require_same_run,
    verify_receipt,
)
from rust_prover_tools.kani_snapshot.publish import (
    GhReleaseApi,
    PublicationRequest,
    publish_release,
)
from rust_prover_tools.kani_snapshot.release_policy import (
    ExpectedRelease,
    ReleaseAction,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sequence,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import argparse

__all__ = (
    "TRUSTED_EVENTS",
    "assemble",
    "build",
    "identity",
    "publish",
    "resolve",
    "validate",
    "workflow_identity",
)

_CANDIDATE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "release",
    "tag_ref",
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
        parsed.append(
            candidate_from_api(
                require_mapping(record["release"], f"candidate {index} release"),
                require_mapping(record["tag_ref"], f"candidate {index} tag_ref"),
                require_sequence(record["checks"], f"candidate {index} checks"),
            )
        )
    return parsed


def _require_manual_inputs(namespace: argparse.Namespace) -> None:
    """Require the two explicit identities a manual resolution needs."""
    if not namespace.commit or not namespace.upstream_tag:
        msg = "manual resolution requires --commit and --upstream-tag"
        raise ProverToolError(msg)


def _manual_resolution(
    namespace: argparse.Namespace,
    workflow: WorkflowIdentity,
) -> Resolution:
    """Build a resolution from an explicit full commit and upstream tag."""
    return Resolution(
        schema_version=1,
        route=ResolutionRoute.MANUAL,
        upstream_commit=resolve_manual_commit(namespace.commit),
        upstream_tag=require_truthy_text(namespace.upstream_tag, "upstream tag"),
        workflow=workflow,
    )


def _scheduled_resolution(
    namespace: argparse.Namespace,
    workflow: WorkflowIdentity,
) -> Resolution:
    """Select the newest qualified upstream release from collected evidence."""
    candidates = _candidates(pathlib.Path(namespace.candidates).read_bytes())
    commit = select_newest_candidate(candidates)
    selected = next(candidate for candidate in candidates if candidate.commit == commit)
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


def _allocated_identity(
    namespace: argparse.Namespace,
    resolution: Resolution,
    builder_commit: str,
) -> PublicationIdentity:
    """Allocate the identity a scheduled run may build and publish."""
    if not namespace.month or not namespace.releases:
        msg = "scheduled identity allocation requires --month and --releases"
        raise ProverToolError(msg)
    tag = allocate_publication_id(
        upstream_commit=resolution.upstream_commit,
        month=namespace.month,
        builder_commit=builder_commit,
        releases=parse_publications(pathlib.Path(namespace.releases).read_bytes()),
    )
    return PublicationIdentity(
        schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
        tag=tag,
        month=namespace.month,
        upstream_commit=resolution.upstream_commit,
        builder_commit=builder_commit,
        workflow=resolution.workflow,
    )


def _manual_identity(
    namespace: argparse.Namespace,
    resolution: Resolution,
    builder_commit: str,
) -> PublicationIdentity:
    """Adopt the tag an operator dispatched, bound to the resolved commit."""
    tag = validate_tag(namespace.publication_id, resolution.upstream_commit)
    month = publication_month(tag)
    if namespace.month and namespace.month != month:
        msg = f"dispatched publication id belongs to {month}, not {namespace.month}"
        raise ProverToolError(msg)
    return PublicationIdentity(
        schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
        tag=tag,
        month=month,
        upstream_commit=resolution.upstream_commit,
        builder_commit=builder_commit,
        workflow=resolution.workflow,
    )


def identity(namespace: argparse.Namespace) -> int:
    """Record the immutable identity this run may publish under."""
    workflow = workflow_identity(namespace)
    resolution = parse_resolution(pathlib.Path(namespace.resolution).read_bytes())
    if not same_run(resolution.workflow, workflow):
        msg = "resolution belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    builder_commit = require_full_sha(
        namespace.builder_commit, "identity builder commit"
    )
    choose = _manual_identity if namespace.publication_id else _allocated_identity
    identity = choose(namespace, resolution, builder_commit)
    write_document(
        pathlib.Path(namespace.output), publication_identity_to_json(identity)
    )
    return 0


def build(namespace: argparse.Namespace) -> int:
    """Derive the pinned build plan from a resolution and its identity."""
    workflow = workflow_identity(namespace)
    resolution = parse_resolution(pathlib.Path(namespace.resolution).read_bytes())
    identity = parse_publication_identity(pathlib.Path(namespace.identity).read_bytes())
    for document in (resolution, identity):
        if not same_run(document.workflow, workflow):
            msg = "producer document belongs to another workflow run or attempt"
            raise ProverToolError(msg)
    if identity.upstream_commit != resolution.upstream_commit:
        msg = "publication identity names another upstream commit"
        raise ProverToolError(msg)
    builder_commit = require_full_sha(
        namespace.builder_commit, "build plan builder_commit"
    )
    if identity.builder_commit != builder_commit:
        msg = "publication identity names another builder commit"
        raise ProverToolError(msg)
    plan = plan_build(
        BuildRequest(
            resolution=resolution,
            workflow=workflow,
            builder_commit=builder_commit,
            tag=identity.tag,
        )
    )
    write_document(pathlib.Path(namespace.output), plan_to_json(plan))
    return 0


def assemble(namespace: argparse.Namespace) -> int:
    """Measure the payloads and write the manifest and checksums."""
    workflow = workflow_identity(namespace)
    inputs = parse_assembly_input(pathlib.Path(namespace.input).read_bytes())
    output = pathlib.Path(namespace.output)
    assembly = assemble_release(
        AssemblyRequest(
            payloads=pathlib.Path(namespace.payloads),
            inputs=inputs,
            workflow=workflow,
        )
    )
    write_document(output / MANIFEST_NAME, assembly.manifest_bytes)
    write_document(output / CHECKSUMS_NAME, assembly.checksums)
    return 0


def validate(namespace: argparse.Namespace) -> int:
    """Check the manifest and payloads, then write same-run evidence."""
    workflow = workflow_identity(namespace)
    parsed = parse_manifest(pathlib.Path(namespace.manifest).read_bytes())
    if not same_run(parsed.manifest.workflow, workflow):
        msg = "manifest belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    verify_release(parsed, pathlib.Path(namespace.payloads))
    checks = tuple(item.strip() for item in namespace.checks.split(",") if item.strip())
    receipt = build_receipt(
        workflow,
        parsed,
        checks,
        verification_passed=True,
    )
    write_document(pathlib.Path(namespace.output), receipt_to_json(receipt))
    return 0


def _expected_release(
    parsed: ParsedManifest, directory: pathlib.Path
) -> ExpectedRelease:
    """Build the immutable identity publication must match."""
    manifest = parsed.manifest
    return ExpectedRelease(
        tag=manifest.tag,
        builder_commit=manifest.builder_commit,
        assets=release_assets(parsed, directory),
        manifest_sha256=parsed.sha256,
        run_id=manifest.workflow.run_id,
        run_attempt=manifest.workflow.run_attempt,
    )


def publish(namespace: argparse.Namespace) -> int:
    """Drive the immutable release to a published, verified state."""
    workflow = workflow_identity(namespace)
    payloads = pathlib.Path(namespace.payloads)
    parsed = parse_manifest(pathlib.Path(namespace.manifest).read_bytes())
    receipt = parse_receipt(pathlib.Path(namespace.receipt).read_bytes())
    require_same_run(receipt, workflow)
    verify_receipt(receipt, parsed)
    verify_release(parsed, payloads)
    expected = _expected_release(parsed, payloads)
    request = PublicationRequest(
        expected=expected,
        validation=receipt.binding(),
        asset_paths={asset.name: payloads / asset.name for asset in expected.assets},
    )
    decision = publish_release(request, GhReleaseApi())
    if decision.action is not ReleaseAction.NOOP:
        msg = f"publication ended without a no-op observation: {decision.reason}"
        raise ProverToolError(msg)
    print(f"published {parsed.manifest.tag} from {REPOSITORY}: {decision.reason}")
    return 0
