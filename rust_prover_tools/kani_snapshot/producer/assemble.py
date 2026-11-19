"""Assemble a snapshot manifest from the exact payload files.

The manifest is the identity a consumer pin hashes, so assembly is the point at
which claims become evidence: every payload is re-read from disk, measured and
hashed here, and the manifest records only those measured bytes. The input
document therefore carries identities and measurements but never asset digests,
and the recorded `asset_bytes` total is recomputed rather than trusted.

`SHA256SUMS` covers the payloads and `manifest.json`, as the design requires.
Neither the manifest nor the checksum file hashes itself.
"""

from __future__ import annotations

import dataclasses as dc
import operator
import stat
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest import manifest_digest, manifest_to_json
from rust_prover_tools.kani_snapshot.models import (
    ASSET_ROLES,
    REPOSITORY,
    SCHEMA_VERSION,
    SUPPORTED_TARGET,
    AssetRecord,
    ResourceObservation,
    SnapshotManifest,
)
from rust_prover_tools.kani_snapshot.producer.assemble_input import (
    AssemblyInput,
    AssemblyRequest,
    assembly_input_to_json,
    parse_assembly_input,
)
from rust_prover_tools.kani_snapshot.producer.commands import (
    bundle_asset_name,
    launcher_asset_name,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle_model import same_run
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import validate_target

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "Assembly",
    "AssemblyInput",
    "AssemblyRequest",
    "assemble",
    "assembly_input_to_json",
    "parse_assembly_input",
    "payload_names",
)

#: Media type of the three archive payloads.
_ARCHIVE_MEDIA_TYPE: typ.Final[str] = "application/gzip"

#: Media type of the separately bounded inventory document.
_INVENTORY_MEDIA_TYPE: typ.Final[str] = "application/json"

#: The name of the rendered manifest inside the release asset set.
MANIFEST_NAME: typ.Final[str] = "manifest.json"


@dc.dataclass(frozen=True, slots=True)
class Assembly:
    """A rendered manifest and the evidence it records.

    Attributes
    ----------
    manifest : SnapshotManifest
        Validated manifest model.
    manifest_bytes : bytes
        Canonical manifest bytes.
    manifest_sha256 : str
        Digest of `manifest_bytes`.
    checksums : bytes
        `SHA256SUMS` rows for the payloads and `manifest.json`.
    """

    manifest: SnapshotManifest
    manifest_bytes: bytes
    manifest_sha256: str
    checksums: bytes


def payload_names(version: str, target: str) -> dict[str, str]:
    """Return the payload basename for every manifest role.

    Parameters
    ----------
    version : str
        Kani package version from the pinned recipe.
    target : str
        The single supported target triple.

    Returns
    -------
    dict[str, str]
        Role-to-basename mapping, so a caller cannot rename an asset.

    Raises
    ------
    ProverToolError
        Raised when the version or target is malformed.
    """
    validated = validate_target(target)
    return {
        "backend": bundle_asset_name(version, validated),
        "launcher": launcher_asset_name(version, validated),
        "notices": f"kani-{version}-notices-{validated}.tar.gz",
        "inventory": "inventory.json",
    }


def _require_payload_directory(payloads: Path) -> None:
    """Require a directory, not a file or a missing path."""
    if not payloads.is_dir():
        msg = f"payload directory '{payloads}' does not exist"
        raise ProverToolError(msg)


def _require_no_missing(present: set[str], wanted: set[str]) -> None:
    """Require every expected payload name to be present."""
    missing = sorted(wanted - present)
    if missing:
        msg = f"payload directory is missing files: {', '.join(missing)}"
        raise ProverToolError(msg)


def _require_no_extras(present: set[str], wanted: set[str]) -> None:
    """Require the directory to carry nothing the manifest does not name."""
    extra = sorted(present - wanted)
    if extra:
        msg = f"payload directory carries unexpected files: {', '.join(extra)}"
        raise ProverToolError(msg)


def _require_regular_payloads(payloads: Path, names: cabc.Iterable[str]) -> None:
    """Require every payload entry to be a regular file, following no links.

    `Path.is_file()` follows a symbolic link, so a link to a regular file
    outside the payload directory would otherwise be admitted and measured.
    Inspecting the entry itself records only files the build job created.
    """
    for name in names:
        path = payloads / name
        try:
            status = path.lstat()
        except OSError as exc:
            msg = f"failed to inspect payload {name!r}: {exc}"
            raise ProverToolError(msg) from exc
        if stat.S_ISLNK(status.st_mode):
            msg = f"payload {name!r} is a symbolic link, which is not admitted"
            raise ProverToolError(msg)
        if not stat.S_ISREG(status.st_mode):
            msg = f"payload {name!r} must be a regular file"
            raise ProverToolError(msg)


def _require_exact_names(payloads: Path, expected: cabc.Mapping[str, str]) -> None:
    """Require exactly the expected payload files, with no extras or links."""
    wanted = set(expected.values())
    present = {entry.name for entry in payloads.iterdir()}
    _require_no_missing(present, wanted)
    _require_no_extras(present, wanted)
    _require_regular_payloads(payloads, sorted(wanted))


def _asset(role: str, name: str, media_type: str, path: Path) -> AssetRecord:
    """Measure one payload and record its exact bytes."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        msg = f"failed to stat payload '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return AssetRecord(
        role=role,
        name=name,
        media_type=media_type,
        size=size,
        sha256=sha256_file(path),
    )


def _payload_assets(
    payloads: Path,
    names: cabc.Mapping[str, str],
) -> tuple[AssetRecord, ...]:
    """Measure every payload in the manifest's role order."""
    media = {
        "backend": _ARCHIVE_MEDIA_TYPE,
        "launcher": _ARCHIVE_MEDIA_TYPE,
        "notices": _ARCHIVE_MEDIA_TYPE,
        "inventory": _INVENTORY_MEDIA_TYPE,
    }
    return tuple(
        _asset(role, names[role], media[role], payloads / names[role])
        for role in ASSET_ROLES
    )


def _checksums(assets: tuple[AssetRecord, ...], manifest_sha256: str) -> bytes:
    """Render `SHA256SUMS` rows for the payloads and the manifest."""
    rows = [(asset.sha256, asset.name) for asset in assets]
    rows.append((manifest_sha256, MANIFEST_NAME))
    return "".join(
        f"{digest}  {name}\n"
        for digest, name in sorted(rows, key=operator.itemgetter(1))
    ).encode("ascii")


def _require_bound_identity(request: AssemblyRequest) -> None:
    """Require the build job's metadata to name the resolve job's identity.

    Assembly signs the manifest, and publication derives the release's target
    from that manifest. The build job compiles upstream source and is therefore
    the least trusted producer, so the tag and both commits are compared with
    the record the resolve job allocated instead of being accepted from the
    build job's own document.
    """
    identity = request.identity
    if not same_run(identity.workflow, request.workflow):
        msg = "publication identity belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    inputs = request.inputs
    if inputs.tag != identity.tag:
        msg = (
            f"assembly input tag {inputs.tag!r} is not the allocated tag "
            f"{identity.tag!r}"
        )
        raise ProverToolError(msg)
    if inputs.upstream_commit != identity.upstream_commit:
        msg = (
            f"assembly input upstream commit {inputs.upstream_commit!r} is not "
            f"the allocated upstream commit {identity.upstream_commit!r}"
        )
        raise ProverToolError(msg)
    if inputs.builder_commit != identity.builder_commit:
        msg = (
            f"assembly input builder commit {inputs.builder_commit!r} is not the "
            f"allocated builder commit {identity.builder_commit!r}"
        )
        raise ProverToolError(msg)


def _pinned_version(inputs: AssemblyInput) -> str:
    """Return the recipe version, refusing any other upstream identity.

    The tag does not carry a version, so payload names come from the pinned
    recipe only after the input's upstream commit agrees with it. Assembly
    therefore refuses a payload set derived from any other release.
    """
    recipe = pinned_recipe()
    if inputs.upstream_commit != recipe.upstream.commit:
        msg = "assembly input names an upstream commit outside the pinned recipe"
        raise ProverToolError(msg)
    return recipe.upstream.crate_version


def assemble(request: AssemblyRequest) -> Assembly:
    """Measure the payloads and render the manifest and checksums.

    Parameters
    ----------
    request : AssemblyRequest
        Payload directory, measured metadata, the allocated identity and the
        assembling run.

    Returns
    -------
    Assembly
        Manifest bytes, their digest and the checksum file.

    Raises
    ------
    ProverToolError
        Raised when the request belongs to another run, the build job's
        metadata does not name the allocated identity, the tag does not bind to
        the upstream commit, or the payload set is not exact.
    """
    if not same_run(request.inputs.workflow, request.workflow):
        msg = "assembly input belongs to another workflow run or attempt"
        raise ProverToolError(msg)
    _require_bound_identity(request)
    inputs = request.inputs
    validate_tag(inputs.tag, inputs.upstream_commit)
    names = payload_names(_pinned_version(inputs), SUPPORTED_TARGET)
    _require_payload_directory(request.payloads)
    _require_exact_names(request.payloads, names)
    assets = _payload_assets(request.payloads, names)
    manifest = SnapshotManifest(
        schema_version=SCHEMA_VERSION,
        repository=REPOSITORY,
        tag=inputs.tag,
        upstream_commit=inputs.upstream_commit,
        builder_commit=inputs.builder_commit,
        target=SUPPORTED_TARGET,
        workflow=inputs.workflow,
        toolchain=inputs.toolchain,
        launcher_package_version=inputs.launcher_package_version,
        backend=inputs.backend,
        runtime=inputs.runtime,
        build=inputs.build,
        prerequisites=inputs.prerequisites,
        cache=inputs.cache,
        resources=ResourceObservation(
            wall_seconds=inputs.wall_seconds,
            disk_high_water_bytes=inputs.disk_high_water_bytes,
            asset_bytes=sum(asset.size for asset in assets),
        ),
        assets=assets,
    )
    rendered = manifest_to_json(manifest)
    digest = manifest_digest(rendered)
    return Assembly(
        manifest=manifest,
        manifest_bytes=rendered,
        manifest_sha256=digest,
        checksums=_checksums(assets, digest),
    )
