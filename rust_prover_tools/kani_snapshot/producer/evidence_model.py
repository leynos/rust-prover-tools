"""The build evidence document: measured identities, locks and resources.

The build job is the only place where the toolchain, backend, lockfiles, cache
and resources are observed. It writes this document, and assembly re-derives
the payload digests itself, so nothing the build job claims about a payload can
reach the manifest. Every identity here is validated with the same parser the
manifest uses, which means an evidence document that would produce an invalid
manifest is refused before a payload is hashed.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import (
    parse_backend,
    parse_cache,
    parse_toolchain,
)
from rust_prover_tools.kani_snapshot.models import (
    BackendIdentity,
    CacheObservation,
    SubmoduleRecord,
    ToolchainIdentity,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.assemble_input import (
    resources_document,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sequence,
    require_sha256,
    require_truthy_text,
)

__all__ = (
    "EVIDENCE_SCHEMA_VERSION",
    "BuildEvidence",
    "evidence_to_json",
    "parse_build_evidence",
)

#: The only evidence schema version this implementation understands.
#:
#: Version 2 adds the observed recursive submodule revisions, which version 1
#: took from the recipe instead of measuring.
EVIDENCE_SCHEMA_VERSION: typ.Final[int] = 2

_EVIDENCE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "toolchain",
    "backend",
    "launcher_package_version",
    "lock_digests",
    "submodules",
    "cache",
    "resources",
})
_LOCK_KEYS: typ.Final[frozenset[str]] = frozenset({"path", "sha256"})
_SUBMODULE_KEYS: typ.Final[frozenset[str]] = frozenset({"path", "commit"})


@dc.dataclass(frozen=True, slots=True)
class BuildEvidence:
    """Everything the build job measured, except the payload bytes.

    Attributes
    ----------
    schema_version : int
        Evidence schema version. Only version 2 is accepted.
    toolchain : ToolchainIdentity
        Exact compiler identities and recorded toolchain files.
    backend : BackendIdentity
        Upstream backend identity and solver digests.
    launcher_package_version : str
        Version reported by the packaged launcher crate.
    lock_digests : tuple[tuple[str, str], ...]
        Recorded lockfile path and digest pairs, in path order.
    submodules : tuple[SubmoduleRecord, ...]
        Recursive submodule revisions measured from the checkout, in path
        order. These are observations, not the recipe's recorded gitlinks.
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
    submodules: tuple[SubmoduleRecord, ...]
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
        submodule_paths = [record.path for record in self.submodules]
        if len(set(submodule_paths)) != len(submodule_paths):
            msg = "build evidence records a submodule path more than once"
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


def _submodules(value: object) -> tuple[SubmoduleRecord, ...]:
    """Parse the submodule revisions measured from the checkout."""
    records: list[SubmoduleRecord] = []
    for index, item in enumerate(require_sequence(value, "build evidence submodules")):
        field = f"build evidence submodules[{index}]"
        document = require_mapping(item, field)
        reject_unknown_keys(document, _SUBMODULE_KEYS, field)
        missing = sorted(_SUBMODULE_KEYS - set(document))
        if missing:
            msg = f"{field} is missing keys: {', '.join(missing)}"
            raise ProverToolError(msg)
        records.append(
            SubmoduleRecord(
                path=require_truthy_text(document["path"], f"{field} path"),
                commit=require_full_sha(document["commit"], f"{field} commit"),
            )
        )
    return tuple(sorted(records, key=lambda record: record.path))


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
        submodules=_submodules(document["submodules"]),
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
        "submodules": [
            {"path": record.path, "commit": record.commit}
            for record in evidence.submodules
        ],
        "cache": {"outcome": evidence.cache.outcome, "key": evidence.cache.key},
        "resources": {
            "wall_seconds": evidence.wall_seconds,
            "disk_high_water_bytes": evidence.disk_high_water_bytes,
        },
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")
