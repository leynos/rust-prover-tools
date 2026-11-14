"""Strict parsing of snapshot manifests and canonical rendering.

The manifest is UTF-8 JSON whose exact bytes are hashed into the consumer pin.
Parsing therefore never reserializes before that digest is checked: callers
verify `ParsedManifest.sha256` against the pin first, then inspect the model.
Field-group parsers live in `manifest_groups`; deterministic rendering lives in
`render` and is re-exported here so callers have one manifest entry point.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import (
    parse_assets,
    parse_backend,
    parse_build,
    parse_cache,
    parse_prerequisite,
    parse_resources,
    parse_runtime,
    parse_toolchain,
    parse_workflow,
)
from rust_prover_tools.kani_snapshot.models import (
    SCHEMA_VERSION,
    SnapshotManifest,
    SnapshotPin,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.render import manifest_to_json
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sequence,
    require_truthy_text,
    validate_repository,
    validate_target,
)

__all__ = (
    "MANIFEST_KEYS",
    "ParsedManifest",
    "manifest_digest",
    "manifest_to_json",
    "parse_manifest",
    "validate_binding",
)

#: Exact top-level key set accepted in schema version 1.
MANIFEST_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "repository",
    "tag",
    "upstream_commit",
    "builder_commit",
    "target",
    "workflow",
    "toolchain",
    "launcher",
    "backend",
    "runtime",
    "build",
    "prerequisites",
    "cache",
    "resources",
    "assets",
})

#: Exact key set accepted inside the `launcher` group.
_LAUNCHER_KEYS: typ.Final[frozenset[str]] = frozenset({"package_version"})


@dc.dataclass(frozen=True, slots=True)
class ParsedManifest:
    """A manifest with the exact bytes and digest a pin approved.

    Attributes
    ----------
    manifest : SnapshotManifest
        Validated model.
    sha256 : str
        SHA-256 of `raw`.
    raw : bytes
        Exact manifest bytes, used for attestation and extraction policy.
    """

    manifest: SnapshotManifest
    sha256: str
    raw: bytes


def manifest_digest(data: bytes) -> str:
    """Return the lowercase SHA-256 of exact manifest bytes.

    Parameters
    ----------
    data : bytes
        Exact manifest bytes, including the final newline.

    Returns
    -------
    str
        Lowercase hexadecimal digest.
    """
    return hashlib.sha256(data).hexdigest()


def _check_schema(value: object) -> int:
    """Require the single supported manifest schema version."""
    version = require_int(value, "manifest schema_version")
    if version != SCHEMA_VERSION:
        msg = f"snapshot manifest schema_version must be {SCHEMA_VERSION}"
        raise ProverToolError(msg)
    return version


def parse_manifest(data: bytes) -> ParsedManifest:
    """Parse exact manifest bytes into validated models.

    Parameters
    ----------
    data : bytes
        Exact manifest bytes.

    Returns
    -------
    ParsedManifest
        Validated model, its digest and the raw bytes.

    Raises
    ------
    ProverToolError
        Raised for invalid UTF-8 or JSON, duplicate keys, non-finite numbers,
        unknown or missing keys, wrong types, malformed identities or an
        inconsistent asset inventory.
    """
    document = require_mapping(strict_json_loads(data, "manifest"), "manifest")
    reject_unknown_keys(document, MANIFEST_KEYS, "manifest")
    missing = sorted(MANIFEST_KEYS - set(document))
    if missing:
        msg = f"snapshot manifest is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    upstream = require_full_sha(document["upstream_commit"], "manifest upstream_commit")
    launcher = require_mapping(document["launcher"], "launcher")
    reject_unknown_keys(launcher, _LAUNCHER_KEYS, "launcher")
    manifest = SnapshotManifest(
        schema_version=_check_schema(document["schema_version"]),
        repository=validate_repository(document["repository"]),
        tag=validate_tag(document["tag"], upstream),
        upstream_commit=upstream,
        builder_commit=require_full_sha(
            document["builder_commit"], "manifest builder_commit"
        ),
        target=validate_target(document["target"]),
        workflow=parse_workflow(
            require_mapping(document["workflow"], "workflow"), "workflow"
        ),
        toolchain=parse_toolchain(
            require_mapping(document["toolchain"], "toolchain"), "toolchain"
        ),
        launcher_package_version=require_truthy_text(
            launcher.get("package_version"), "launcher package_version"
        ),
        backend=parse_backend(
            require_mapping(document["backend"], "backend"), "backend"
        ),
        runtime=parse_runtime(
            require_mapping(document["runtime"], "runtime"), "runtime"
        ),
        build=parse_build(require_mapping(document["build"], "build"), "build"),
        prerequisites=tuple(
            parse_prerequisite(item)
            for item in require_sequence(document["prerequisites"], "prerequisites")
        ),
        cache=parse_cache(require_mapping(document["cache"], "cache"), "cache"),
        resources=parse_resources(
            require_mapping(document["resources"], "resources"), "resources"
        ),
        assets=parse_assets(document["assets"]),
    )
    return ParsedManifest(
        manifest=manifest,
        sha256=manifest_digest(data),
        raw=data,
    )


def validate_binding(pin: SnapshotPin, manifest: SnapshotManifest) -> None:
    """Require a manifest to describe exactly the snapshot a pin names.

    Parameters
    ----------
    pin : SnapshotPin
        Reviewed consumer pin.
    manifest : SnapshotManifest
        Validated manifest.

    Raises
    ------
    ProverToolError
        Raised when any identity field differs.
    """
    checks = (
        ("repository", pin.repository, manifest.repository),
        ("tag", pin.tag, manifest.tag),
        ("upstream_commit", pin.upstream_commit, manifest.upstream_commit),
        ("builder_commit", pin.builder_commit, manifest.builder_commit),
        ("target", pin.target, manifest.target),
    )
    for field, pinned, described in checks:
        if pinned != described:
            msg = (
                f"snapshot manifest {field} does not match the pinned identity: "
                f"pin has {pinned}, manifest has {described}"
            )
            raise ProverToolError(msg)
    # A backend built from any other commit is not the snapshot's Kani, even
    # when its version string matches. Treating a contradiction as valid would
    # let a manifest describe two different upstream identities at once.
    if manifest.backend.revision != manifest.upstream_commit:
        msg = (
            "snapshot manifest backend revision does not name the upstream "
            f"commit: upstream has {manifest.upstream_commit}, "
            f"backend has {manifest.backend.revision}"
        )
        raise ProverToolError(msg)
