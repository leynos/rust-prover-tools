"""Deterministic cache identity for validated snapshot pins.

Every field that selects bytes or a compiler is part of the key. The reported
Kani semantic version is deliberately absent: two snapshots can share one
upstream package version while naming different compilers, and treating that
label as an identity would let one replace the other.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = ("identity_key",)


def identity_key(pin: SnapshotPin) -> str:
    """Return the unambiguous cache key for one validated pin.

    Parameters
    ----------
    pin : SnapshotPin
        Validated pin.

    Returns
    -------
    str
        Lowercase SHA-256 of the canonical identity document. The document is
        canonical JSON with sorted keys, so two pins with the same content
        always produce the same key.
    """
    document = {
        "schema_version": pin.schema_version,
        "repository": pin.repository,
        "tag": pin.tag,
        "upstream_commit": pin.upstream_commit,
        "builder_commit": pin.builder_commit,
        "target": pin.target,
        "manifest_sha256": pin.manifest_sha256,
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
