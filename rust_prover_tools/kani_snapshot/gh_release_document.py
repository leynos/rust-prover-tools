"""Parse and verify the release documents the GitHub adapter reads.

The adapter in `gh_release_api` owns command execution and the publication
state machine; this module owns the answer: turning one API document into an
`ObservedRelease` without trusting its digests, and re-verifying a published
release's own manifest, checksums, payloads, attestation and identity. Keeping
parsing here means the adapter cannot accidentally treat a claimed digest as
evidence.

A published release is authenticated by the same verifier a consumer installs
with: the bundle must satisfy the pinned certificate, workflow, ref and digest
policy, and the verified statement must name this manifest's own run and
attempt. Structural checks alone would accept an unsigned statement, so they
run first as a cheap refusal and never on their own decide
`provenance_verified`. Exercising the verifier against a real clean-runner
signature remains EP-M5.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_attestation import (
    require_attestation,
    require_verified_signature,
)
from rust_prover_tools.kani_snapshot.integrity import (
    verify_asset,
    verify_checksums,
)
from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.producer.assemble import MANIFEST_NAME
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.release_assets import (
    CHECKSUMS_NAME,
    EVIDENCE_NAMES,
)
from rust_prover_tools.kani_snapshot.release_model import (
    ObservedAsset,
    ObservedRelease,
    ReleaseTransientError,
    parse_input_marker,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.manifest import ParsedManifest

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
    return ObservedAsset(
        name=name,
        size=size,
        sha256=sha256,
        asset_id=_asset_id(record.get("id")),
    )


def _asset_id(value: object) -> int | None:
    """Return an asset's server identifier, refusing a malformed one.

    The identifier is only used to delete a record left behind by a failed
    upload, so an observation that does not report one is still usable; a
    reported identifier that is not a positive integer, however, is a response
    this adapter does not understand.
    """
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        msg = "release asset id must be a positive integer"
        raise ProverToolError(msg)
    if value <= 0:
        msg = "release asset id must be a positive integer"
        raise ProverToolError(msg)
    return value


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


def _require_bound_identity(
    parsed: ParsedManifest,
    *,
    tag: str,
    body: str | None,
) -> None:
    """Require the manifest to be the release's own recorded identity.

    The release body advertises the build-input fingerprint; the manifest
    records the commits and recipe those inputs are hashed from. Recomputing
    the fingerprint from the manifest and comparing it with the body is what
    binds the published bytes to the identity a later run is allowed to reuse.
    """
    if parsed.manifest.tag != tag:
        msg = f"published manifest records tag {parsed.manifest.tag!r}, not {tag!r}"
        raise ProverToolError(msg)
    marker = parse_input_marker(body)
    if marker is None:
        msg = "published release body does not record its build inputs"
        raise ProverToolError(msg)
    expected = input_fingerprint(
        upstream_commit=parsed.manifest.upstream_commit,
        builder_commit=parsed.manifest.builder_commit,
        recipe=pinned_recipe(),
    )
    if marker != expected:
        msg = "published manifest does not match the release's recorded build inputs"
        raise ProverToolError(msg)


def _verify_release(directory: Path, tag: str, body: str | None) -> None:
    """Require a published release's bytes, evidence and identity to agree."""
    parsed = parse_manifest((directory / MANIFEST_NAME).read_bytes())
    _require_bound_identity(parsed, tag=tag, body=body)
    verify_checksums(
        directory / CHECKSUMS_NAME,
        parsed.manifest,
        parsed.sha256,
    )
    for asset in parsed.manifest.assets:
        verify_asset(directory / asset.name, asset)
    require_attestation(directory, parsed.sha256)
    require_verified_signature(directory, parsed)


def verify_publication(
    directory: Path,
    names: cabc.Sequence[str],
    *,
    tag: str,
    body: str | None,
) -> str | None:
    """Return why a release does not authenticate its own publication.

    The served manifest is the authority for the served bytes: its checksums
    must describe it and each payload it names, every evidence asset must be
    present, its attestation must name its digest under the consumer's own
    policy and this run, and its recorded identities must reproduce the
    build-input fingerprint the release body advertises. A verification failure
    is reported as a reason rather than raised, because the policy owns what an
    unverifiable release means — but the reason travels with it, so a refusal
    can name the actual disagreement instead of only that verification failed.

    Parameters
    ----------
    directory : Path
        Downloaded assets of the release.
    names : collections.abc.Sequence of str
        Asset names the release serves.
    tag : str
        Immutable tag the verification was made for.
    body : str | None
        Release body, which records the build-input fingerprint.

    Returns
    -------
    str | None
        `None` when every check passed, otherwise a bounded description of the
        first disagreement.

    Raises
    ------
    ReleaseTransientError
        Raised when the attestation verifier itself exceeds its budget. A
        timed-out verifier may succeed on a fresh observation, so it must not
        be reported as an unverified release.
    """
    missing = sorted(set(EVIDENCE_NAMES) - set(names))
    if missing:
        return f"release does not serve its evidence assets: {', '.join(missing)}"
    try:
        _verify_release(directory, tag, body)
    except ReleaseTransientError:
        # Checked before the base class: a retryable transport failure is not
        # the same answer as a release whose evidence does not verify.
        raise
    except ProverToolError as exc:
        # A release whose evidence does not verify is simply not verified; the
        # caller decides whether that is a conflict or a retry.
        return str(exc)
    except OSError as exc:
        # An unreadable release is equally unverified. The clauses stay
        # separate because the project's pylint runs under Python 3.12, which
        # cannot parse the parenthesised-pair shorthand the formatter prefers.
        return f"release bytes could not be read: {exc}"
    except ValueError as exc:
        # Malformed JSON or base64 in an external document is unverified too.
        # `json.JSONDecodeError` and `binascii.Error` are both `ValueError`.
        return f"release document is malformed: {exc}"
    return None
