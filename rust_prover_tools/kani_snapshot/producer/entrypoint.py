"""Developer entrypoint for the guarded producer workflow.

Each subcommand is one job's whole effect: it reads the typed documents and
files earlier jobs produced, validates their identity and same-run binding, and
only then writes its output. These are internal automation interfaces, not
consumer APIs, so they favour failing closed over convenience.

Run `python -m rust_prover_tools.kani_snapshot.producer.entrypoint --help` for
the argument list of each command. `resolve` selects an upstream commit,
`build` derives the pinned plan, `assemble` hashes the payloads into a
manifest, `validate` writes same-run evidence, and `publish` drives the
immutable release.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest import ParsedManifest, parse_manifest
from rust_prover_tools.kani_snapshot.models import REPOSITORY, WorkflowIdentity
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.assemble import (
    MANIFEST_NAME,
    AssemblyRequest,
    assemble,
    parse_assembly_input,
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
    verify_payloads,
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
    ReleaseAsset,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_sequence,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("main",)

_CANDIDATE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "release",
    "tag_ref",
    "checks",
})
_TRUSTED_EVENTS: typ.Final[tuple[str, ...]] = (
    "schedule",
    "workflow_dispatch",
)


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the trusted run identity every operation requires."""
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--event", choices=_TRUSTED_EVENTS, required=True)
    parser.add_argument("--run-url", default=None)


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


def _write(path: pathlib.Path, data: bytes) -> None:
    """Write bytes atomically so a reader never sees a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    os.close(handle)
    try:
        with pathlib.Path(name).open("wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        pathlib.Path(name).replace(path)
    except OSError as exc:
        pathlib.Path(name).unlink(missing_ok=True)
        msg = f"failed to write '{path}': {exc}"
        raise ProverToolError(msg) from exc


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


def _resolve(namespace: argparse.Namespace) -> int:
    """Select the upstream commit this run may build."""
    workflow = _workflow(namespace)
    if namespace.route == ResolutionRoute.MANUAL.value:
        _require_manual_inputs(namespace)
        resolution = _manual_resolution(namespace, workflow)
    else:
        if not namespace.candidates:
            msg = "scheduled resolution requires --candidates"
            raise ProverToolError(msg)
        resolution = _scheduled_resolution(namespace, workflow)
    _write(pathlib.Path(namespace.output), resolution_to_json(resolution))
    return 0


def _build(namespace: argparse.Namespace) -> int:
    """Derive the pinned build plan from a resolution."""
    workflow = _workflow(namespace)
    resolution = parse_resolution(pathlib.Path(namespace.resolution).read_bytes())
    if not same_run(resolution.workflow, workflow):
        msg = "resolution belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    plan = plan_build(
        BuildRequest(
            resolution=resolution,
            workflow=workflow,
            builder_commit=namespace.builder_commit,
            tag=namespace.tag,
        )
    )
    _write(pathlib.Path(namespace.output), plan_to_json(plan))
    return 0


def _assemble(namespace: argparse.Namespace) -> int:
    """Measure the payloads and write the manifest and checksums."""
    workflow = _workflow(namespace)
    inputs = parse_assembly_input(pathlib.Path(namespace.input).read_bytes())
    output = pathlib.Path(namespace.output)
    assembly = assemble(
        AssemblyRequest(
            payloads=pathlib.Path(namespace.payloads),
            inputs=inputs,
            workflow=workflow,
        )
    )
    _write(output / MANIFEST_NAME, assembly.manifest_bytes)
    _write(output / "SHA256SUMS", assembly.checksums)
    return 0


def _validate(namespace: argparse.Namespace) -> int:
    """Check the manifest and payloads, then write same-run evidence."""
    workflow = _workflow(namespace)
    parsed = parse_manifest(pathlib.Path(namespace.manifest).read_bytes())
    if not same_run(parsed.manifest.workflow, workflow):
        msg = "manifest belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    verify_payloads(parsed, pathlib.Path(namespace.payloads))
    checks = tuple(item.strip() for item in namespace.checks.split(",") if item.strip())
    receipt = build_receipt(
        workflow,
        parsed,
        checks,
        verification_passed=True,
    )
    _write(pathlib.Path(namespace.output), receipt_to_json(receipt))
    return 0


def _expected_release(parsed: ParsedManifest) -> ExpectedRelease:
    """Build the immutable identity publication must match."""
    manifest = parsed.manifest
    return ExpectedRelease(
        tag=manifest.tag,
        builder_commit=manifest.builder_commit,
        assets=tuple(
            ReleaseAsset(name=asset.name, size=asset.size, sha256=asset.sha256)
            for asset in manifest.assets
        ),
        manifest_sha256=parsed.sha256,
        run_id=manifest.workflow.run_id,
        run_attempt=manifest.workflow.run_attempt,
    )


def _publish(namespace: argparse.Namespace) -> int:
    """Drive the immutable release to a published, verified state."""
    workflow = _workflow(namespace)
    payloads = pathlib.Path(namespace.payloads)
    parsed = parse_manifest(pathlib.Path(namespace.manifest).read_bytes())
    receipt = parse_receipt(pathlib.Path(namespace.receipt).read_bytes())
    require_same_run(receipt, workflow)
    verify_receipt(receipt, parsed)
    verify_payloads(parsed, payloads)
    request = PublicationRequest(
        expected=_expected_release(parsed),
        validation=receipt.binding(),
        asset_paths={
            asset.name: payloads / asset.name for asset in parsed.manifest.assets
        },
    )
    decision = publish_release(request, GhReleaseApi())
    if decision.action is not ReleaseAction.NOOP:
        msg = f"publication ended without a no-op observation: {decision.reason}"
        raise ProverToolError(msg)
    print(f"published {parsed.manifest.tag} from {REPOSITORY}: {decision.reason}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for every producer operation."""
    parser = argparse.ArgumentParser(prog="kani-snapshot-producer")
    commands = parser.add_subparsers(dest="operation", required=True)

    resolve = commands.add_parser("resolve", help="select an upstream commit")
    _add_workflow_arguments(resolve)
    resolve.add_argument(
        "--route", choices=[route.value for route in ResolutionRoute], required=True
    )
    resolve.add_argument("--commit", default=None)
    resolve.add_argument("--upstream-tag", default=None)
    resolve.add_argument("--candidates", default=None)
    resolve.add_argument("--output", required=True)
    resolve.set_defaults(handler=_resolve)

    build = commands.add_parser("build", help="derive the pinned build plan")
    _add_workflow_arguments(build)
    build.add_argument("--resolution", required=True)
    build.add_argument("--builder-commit", required=True)
    build.add_argument("--tag", required=True)
    build.add_argument("--output", required=True)
    build.set_defaults(handler=_build)

    assembly = commands.add_parser("assemble", help="hash payloads into a manifest")
    _add_workflow_arguments(assembly)
    assembly.add_argument("--input", required=True)
    assembly.add_argument("--payloads", required=True)
    assembly.add_argument("--output", required=True)
    assembly.set_defaults(handler=_assemble)

    validate = commands.add_parser(
        "validate", help="write same-run validation evidence"
    )
    _add_workflow_arguments(validate)
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--payloads", required=True)
    validate.add_argument("--checks", default="manifest-digest,payload-digest")
    validate.add_argument("--output", required=True)
    validate.set_defaults(handler=_validate)

    publish = commands.add_parser("publish", help="publish the immutable release")
    _add_workflow_arguments(publish)
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--receipt", required=True)
    publish.add_argument("--payloads", required=True)
    publish.set_defaults(handler=_publish)
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run one producer operation and report a user-facing failure."""
    namespace = build_parser().parse_args(argv)
    try:
        return typ.cast("cabc.Callable[[argparse.Namespace], int]", namespace.handler)(
            namespace
        )
    except ProverToolError as exc:
        print(f"kani-snapshot-producer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
