"""The asset names one snapshot release must carry, derived without I/O.

A manifest names four payloads; a release also carries the manifest, its
checksums and the build attestation. Assembly, publication and the receipt all
need the same ordering, so the names are derived here once, from validated
records, rather than being repeated by a workflow step that could drift. This
module is the part of the asset set that can be decided by reading a manifest:
the bytes are measured and verified by the orchestrator, which is the layer
allowed to touch a path.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_snapshot.producer.commands import (
    bundle_asset_name,
    launcher_asset_name,
)
from rust_prover_tools.kani_snapshot.validation import validate_target

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest

__all__ = (
    "ATTESTATION_NAME",
    "CHECKSUMS_NAME",
    "EVIDENCE_NAMES",
    "MANIFEST_NAME",
    "PERFORMED_CHECKS",
    "payload_names",
    "release_asset_names",
)

#: The name of the rendered manifest inside the release asset set.
MANIFEST_NAME: typ.Final[str] = "manifest.json"

#: The checksum document assembly writes beside the manifest.
CHECKSUMS_NAME: typ.Final[str] = "SHA256SUMS"

#: The attestation bundle the assembly job produces for the manifest.
ATTESTATION_NAME: typ.Final[str] = "manifest.sigstore.jsonl"

#: Assets derived from the payloads and the manifest, in publication order.
EVIDENCE_NAMES: typ.Final[tuple[str, str, str]] = (
    MANIFEST_NAME,
    CHECKSUMS_NAME,
    ATTESTATION_NAME,
)

#: The checks `verify_release` performs, in the order a receipt records them.
#:
#: The validate operation records this tuple instead of a label list supplied
#: on the command line, so a receipt can only name checks the code actually
#: performed. The names are part of the receipt document's vocabulary.
PERFORMED_CHECKS: typ.Final[tuple[str, ...]] = (
    "manifest-digest",
    "payload-digest",
    "checksums",
    "attestation",
)


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


def release_asset_names(manifest: SnapshotManifest) -> tuple[str, ...]:
    """Return every asset name a release of `manifest` must serve.

    Parameters
    ----------
    manifest : SnapshotManifest
        Validated manifest whose payload assets were measured by assembly.

    Returns
    -------
    tuple[str, ...]
        Payload asset names in manifest order, followed by the fixed evidence
        assets in `EVIDENCE_NAMES` order (`manifest.json`, `SHA256SUMS`, then
        the attestation). Uploads, checksum rows and receipt asset sets all
        derive from this ordering, so callers must preserve it rather than
        treating the result as an unordered set.
    """
    return tuple(asset.name for asset in manifest.assets) + EVIDENCE_NAMES
