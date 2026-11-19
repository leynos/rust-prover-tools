"""Parse and verify the release documents the GitHub adapter reads.

The adapter in `gh_release_api` owns command execution and the publication
state machine; this module owns the answer: turning one API document into an
`ObservedRelease` without trusting its digests, and re-verifying a published
release's own manifest, checksums and payload bytes. Keeping parsing here means
the adapter cannot accidentally treat a claimed digest as evidence.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.integrity import (
    verify_asset,
    verify_checksums,
)
from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.producer.assemble import MANIFEST_NAME
from rust_prover_tools.kani_snapshot.producer.release_assets import (
    CHECKSUMS_NAME,
    EVIDENCE_NAMES,
)
from rust_prover_tools.kani_snapshot.release_model import (
    ObservedAsset,
    ObservedRelease,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("decoded", "parse_release", "verify_publication")

#: Width of the hexadecimal part of a SHA-256 digest.
_SHA256_HEX_LENGTH: typ.Final[int] = 64


def _is_sha256_hex(candidate: str) -> bool:
    """Return whether `candidate` is lowercase hexadecimal of the right width."""
    return len(candidate) == _SHA256_HEX_LENGTH and all(
        char in "0123456789abcdef" for char in candidate
    )


def _require_asset_size(name: str, size: object) -> int:
    """Return a validated asset size, refusing booleans and negatives."""
    msg = f"release asset {name!r} size must be a non-negative integer"
    if not isinstance(size, int) or isinstance(size, bool):
        raise ProverToolError(msg)
    if size < 0:
        raise ProverToolError(msg)
    return size


def _parse_asset(value: object) -> ObservedAsset:
    """Parse one asset record, refusing a malformed size or digest."""
    if not isinstance(value, dict):
        msg = "release asset API response must be an object"
        raise ProverToolError(msg)
    record = typ.cast("dict[str, object]", value)
    name = record.get("name")
    if not isinstance(name, str) or not name:
        msg = "release asset name must be a non-empty string"
        raise ProverToolError(msg)
    size = _require_asset_size(name, record.get("size"))
    digest = record.get("digest")
    sha256: str | None = None
    if isinstance(digest, str) and digest.startswith("sha256:"):
        candidate = digest.removeprefix("sha256:")
        if _is_sha256_hex(candidate):
            sha256 = candidate
    return ObservedAsset(name=name, size=size, sha256=sha256)


def parse_release(document: object) -> ObservedRelease:
    """Parse one release API document into an observation without digests."""
    if not isinstance(document, dict):
        msg = "release API response must be an object"
        raise ProverToolError(msg)
    record = typ.cast("dict[str, object]", document)
    release_id = record.get("id")
    draft = record.get("draft")
    tag = record.get("tag_name")
    target = record.get("target_commitish")
    assets = record.get("assets")
    body = record.get("body")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        msg = "release API response id must be an integer"
        raise ProverToolError(msg)
    if not isinstance(draft, bool):
        msg = "release API response draft must be a boolean"
        raise ProverToolError(msg)
    if not isinstance(tag, str) or not isinstance(target, str):
        msg = "release API response tag and target must be strings"
        raise ProverToolError(msg)
    if not isinstance(assets, list):
        msg = "release API response assets must be an array"
        raise ProverToolError(msg)
    if body is not None and not isinstance(body, str):
        msg = "release API response body must be a string or null"
        raise ProverToolError(msg)
    parsed = tuple(_parse_asset(asset) for asset in typ.cast("list[object]", assets))
    return ObservedRelease(
        exists=True,
        is_draft=draft,
        tag=tag,
        target_commitish=target,
        assets=parsed,
        release_id=release_id,
        body=body,
    )


def decoded(result_stdout: str) -> object:
    """Decode a JSON response, refusing anything that is not JSON."""
    try:
        return json.loads(result_stdout)
    except json.JSONDecodeError as exc:
        msg = f"release API response is not JSON: {exc}"
        raise ProverToolError(msg) from exc


def verify_publication(directory: Path, names: cabc.Sequence[str]) -> bool:
    """Return whether a published release verifies against its own manifest.

    The published manifest is the authority for the published bytes: its
    checksums must describe it and each payload it names, and every evidence
    asset must be present. A verification failure is reported as an
    unverified observation rather than raised, because the policy owns what
    an unverifiable publication means.
    """
    if not set(EVIDENCE_NAMES) <= set(names):
        return False
    try:
        parsed = parse_manifest((directory / MANIFEST_NAME).read_bytes())
        verify_checksums(
            directory / CHECKSUMS_NAME,
            parsed.manifest,
            parsed.sha256,
        )
        for asset in parsed.manifest.assets:
            verify_asset(directory / asset.name, asset)
    except ProverToolError:
        # A release whose evidence does not verify is simply not verified; the
        # caller decides whether that is a conflict or a retry.
        return False
    except OSError:
        # An unreadable release is equally unverified. The clauses stay
        # separate because the project's pylint runs under Python 3.12, which
        # cannot parse the parenthesised-pair shorthand the formatter prefers.
        return False
    return True
