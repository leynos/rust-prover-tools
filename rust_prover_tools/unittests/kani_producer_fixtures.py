"""Shared builders for the guarded producer tests.

The producer refuses any upstream identity except the pinned recipe's, so these
fixtures derive the tag, version and payload names from that recipe rather than
hard-coding them. A recipe bump therefore updates the fixtures with production
instead of leaving tests behind on a stale identity.
"""

from __future__ import annotations

import base64
import json
import typing as typ

from rust_prover_tools.kani_snapshot.attestation import ATTESTATION_PREDICATE
from rust_prover_tools.kani_snapshot.manifest import ParsedManifest, parse_manifest
from rust_prover_tools.kani_snapshot.models import (
    REPOSITORY,
    SUPPORTED_TARGET,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    ComponentRecord,
    FileRecord,
    SolverRecord,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.orchestrator.assembly import (
    Assembly,
    AssemblyInput,
    AssemblyRequest,
    assemble,
    payload_names,
)
from rust_prover_tools.kani_snapshot.orchestrator.build_evidence import BuildEvidence
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildPlan,
    BuildRequest,
    Resolution,
    ResolutionRoute,
    plan_build,
)
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    PUBLICATION_IDENTITY_SCHEMA_VERSION,
    PublicationIdentity,
)
from rust_prover_tools.kani_snapshot.producer.recipe import (
    pinned_recipe,
    recipe_digest,
)

if typ.TYPE_CHECKING:
    import pathlib

PINNED = pinned_recipe()

#: The pinned upstream commit every valid producer input names.
UPSTREAM_COMMIT: typ.Final[str] = PINNED.upstream.commit

#: A builder commit distinct from the upstream one.
BUILDER_COMMIT: typ.Final[str] = "b" * 40

#: The Kani package version the pinned recipe records.
CRATE_VERSION: typ.Final[str] = PINNED.upstream.crate_version

#: An immutable tag bound to the pinned upstream commit.
TAG: typ.Final[str] = f"kani-snapshot-2026-09-{UPSTREAM_COMMIT[:12]}-r1"

#: Payload bytes keyed by manifest role. Distinct content per role so a
#: mis-routed file is detected by its digest, not only by its name.
PAYLOAD_BYTES: typ.Final[dict[str, bytes]] = {
    "backend": b"pinned kani backend payload",
    "launcher": b"pinned launcher payload",
    "notices": b"pinned notices payload",
    "inventory": b'{"entries": ["cbmc", "kani", "kissat", "rustc"]}',
}


def workflow(
    *,
    event: str = "schedule",
    run_id: int = 12345,
    run_attempt: int = 1,
) -> WorkflowIdentity:
    """Return the trusted producer identity, applying the given overrides."""
    return WorkflowIdentity(
        path=WORKFLOW_PATH,
        event=event,
        ref=WORKFLOW_REF,
        run_id=run_id,
        run_attempt=run_attempt,
        run_url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    )


def resolution(
    *,
    route: ResolutionRoute = ResolutionRoute.SCHEDULED,
    identity: WorkflowIdentity | None = None,
    upstream_commit: str = UPSTREAM_COMMIT,
    upstream_tag: str = PINNED.upstream.tag,
) -> Resolution:
    """Return a valid resolution for one trusted run."""
    event = "schedule" if route is ResolutionRoute.SCHEDULED else "workflow_dispatch"
    return Resolution(
        schema_version=1,
        route=route,
        upstream_commit=upstream_commit,
        upstream_tag=upstream_tag,
        workflow=identity or workflow(event=event),
    )


def toolchain() -> ToolchainIdentity:
    """Return the exact compiler identity the pinned recipe records."""
    return ToolchainIdentity(
        identifier=PINNED.upstream.toolchain_channel,
        rustc_vv=PINNED.upstream.rustc_version,
        kani_compiler=f"kani-compiler {CRATE_VERSION}",
        channel_metadata_sha256="4" * 64,
        components=(
            ComponentRecord(
                name="rust-src",
                version=PINNED.upstream.toolchain_channel,
                url=(
                    "https://static.rust-lang.org/dist/"
                    f"{PINNED.upstream.toolchain_channel}/rust-src.tar.gz"
                ),
                size=4096,
                sha256="3" * 64,
            ),
        ),
        rustc_sha256="2" * 64,
        rustup_components_sha256="1" * 64,
        libraries=(
            FileRecord(
                path="lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd.so",
                sha256="0" * 64,
            ),
        ),
    )


def build_plan(
    *,
    identity: WorkflowIdentity | None = None,
    builder_commit: str = BUILDER_COMMIT,
    tag: str = TAG,
) -> BuildPlan:
    """Return the pinned plan this run would build from."""
    measured = workflow() if identity is None else identity
    return plan_build(
        BuildRequest(
            resolution=resolution(identity=measured),
            workflow=measured,
            builder_commit=builder_commit,
            tag=tag,
        )
    )


def evidence(
    *,
    wall_seconds: float = 1800.0,
    disk_high_water_bytes: int = 8_000_000_000,
) -> BuildEvidence:
    """Return measured build evidence for the pinned recipe."""
    return BuildEvidence(
        schema_version=2,
        toolchain=toolchain(),
        backend=BackendIdentity(
            version=CRATE_VERSION,
            revision=UPSTREAM_COMMIT,
            solvers=(SolverRecord("cbmc", "6.11.0", "5" * 64),),
        ),
        launcher_package_version=CRATE_VERSION,
        lock_digests=(("Cargo.lock", "8" * 64),),
        submodules=PINNED.submodules,
        cache=CacheObservation("miss", f"kani-{PINNED.upstream.toolchain_channel}"),
        wall_seconds=wall_seconds,
        disk_high_water_bytes=disk_high_water_bytes,
    )


def assembly_input(
    *,
    identity: WorkflowIdentity | None = None,
    upstream_commit: str = UPSTREAM_COMMIT,
    wall_seconds: float = 1800.0,
) -> AssemblyInput:
    """Return measured assembly metadata for the pinned recipe."""
    measured = workflow() if identity is None else identity
    return AssemblyInput(
        schema_version=1,
        tag=TAG,
        upstream_commit=upstream_commit,
        builder_commit=BUILDER_COMMIT,
        workflow=measured,
        toolchain=toolchain(),
        launcher_package_version=CRATE_VERSION,
        backend=BackendIdentity(
            version=CRATE_VERSION,
            revision=upstream_commit,
            solvers=(SolverRecord("cbmc", "6.11.0", "5" * 64),),
        ),
        runtime=PINNED.runtime,
        build=BuildInputs(
            submodules=PINNED.submodules,
            lock_digests=(("Cargo.lock", "8" * 64),),
            recipe_digest=recipe_digest(PINNED),
        ),
        prerequisites=PINNED.prerequisites,
        cache=CacheObservation("miss", f"kani-{PINNED.upstream.toolchain_channel}"),
        wall_seconds=wall_seconds,
        disk_high_water_bytes=8_000_000_000,
    )


def publication_identity(
    *,
    identity: WorkflowIdentity | None = None,
    tag: str = TAG,
    upstream_commit: str = UPSTREAM_COMMIT,
    builder_commit: str = BUILDER_COMMIT,
) -> PublicationIdentity:
    """Return the identity the resolve job allocates for the pinned build."""
    measured = workflow() if identity is None else identity
    return PublicationIdentity(
        schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
        tag=tag,
        month="2026-09",
        upstream_commit=upstream_commit,
        builder_commit=builder_commit,
        input_fingerprint=input_fingerprint(
            upstream_commit=upstream_commit,
            builder_commit=builder_commit,
            recipe=PINNED,
        ),
        workflow=measured,
    )


def names() -> dict[str, str]:
    """Return the role-to-basename payload mapping for the pinned recipe."""
    return payload_names(CRATE_VERSION, SUPPORTED_TARGET)


def assemble_into(directory: pathlib.Path) -> Assembly:
    """Write the pinned payloads and assemble them for the trusted run."""
    write_payloads(directory)
    return assemble(
        AssemblyRequest(
            payloads=directory,
            inputs=assembly_input(),
            identity=publication_identity(),
            workflow=workflow(),
        )
    )


def parsed_manifest(directory: pathlib.Path) -> ParsedManifest:
    """Return the parsed manifest assembled from the pinned payloads."""
    return parse_manifest(assemble_into(directory).manifest_bytes)


def attestation_bundle(manifest_sha256: str) -> bytes:
    """Return a Sigstore bundle whose statement covers `manifest_sha256`.

    This models what `actions/attest-build-provenance` emits: a DSSE envelope
    whose base64 payload is an in-toto statement naming the manifest's digest.
    Signature validity under a trusted root is a consumer concern, so the
    signature here is present but unchecked.
    """
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "manifest.json", "digest": {"sha256": manifest_sha256}}],
        "predicateType": ATTESTATION_PREDICATE,
        "predicate": {"buildDefinition": {}},
    }
    payload = base64.b64encode(json.dumps(statement).encode("utf-8")).decode("ascii")
    return json.dumps({
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "dsseEnvelope": {
            "payload": payload,
            "payloadType": "application/vnd.in-toto+json",
            "signatures": [{"sig": "c2ln"}],
        },
    }).encode("ascii")


def write_payloads(directory: pathlib.Path) -> dict[str, pathlib.Path]:
    """Write one payload file per role and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, pathlib.Path] = {}
    for role, name in names().items():
        path = directory / name
        path.write_bytes(PAYLOAD_BYTES[role])
        paths[role] = path
    return paths
