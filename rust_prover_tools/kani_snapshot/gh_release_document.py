"""Parse and verify the release documents the GitHub adapter reads.

The adapter in `gh_release_api` owns command execution and the publication
state machine; this module owns the answer: turning one API document into an
`ObservedRelease` without trusting its digests, and re-verifying a published
release's own manifest, checksums, payloads, attestation and identity. Keeping
parsing here means the adapter cannot accidentally treat a claimed digest as
evidence.

A published release is authenticated as far as is possible without a trusted
root: its attestation must be a well-formed DSSE bundle whose statement names
this manifest digest, and its manifest must record the tag and build-input
fingerprint the release body advertises. Whether that signature chains to a
trusted certificate root is a consumer concern, exercised on a clean runner in
EP-M5; a publisher that claimed more here would be recording a check nobody
ran.
"""

from __future__ import annotations

import base64
import json
import typing as typ
from pathlib import PurePosixPath

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.attestation import ATTESTATION_PREDICATE
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
    ATTESTATION_NAME,
    CHECKSUMS_NAME,
    EVIDENCE_NAMES,
)
from rust_prover_tools.kani_snapshot.release_model import (
    ObservedAsset,
    ObservedRelease,
    parse_input_marker,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import require_mapping, require_sequence

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


#: In-toto statement versions GitHub's attestation action emits.
_STATEMENT_TYPES: typ.Final[frozenset[str]] = frozenset({
    "https://in-toto.io/Statement/v1",
    "https://in-toto.io/Statement/v0.1",
})


def _attestation_statement(directory: Path) -> dict[str, object]:
    """Return the in-toto statement a release's attestation bundle carries."""
    bundle = require_mapping(
        strict_json_loads(
            (directory / ATTESTATION_NAME).read_bytes(), "attestation bundle"
        ),
        "attestation bundle",
    )
    envelope = require_mapping(bundle.get("dsseEnvelope"), "attestation envelope")
    payload = envelope.get("payload")
    signatures = envelope.get("signatures")
    if not isinstance(payload, str) or not payload:
        msg = "attestation bundle carries no DSSE payload"
        raise ProverToolError(msg)
    if not isinstance(signatures, list) or not signatures:
        msg = "attestation bundle carries no signature"
        raise ProverToolError(msg)
    try:
        statement = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        msg = f"attestation payload is not base64: {exc}"
        raise ProverToolError(msg) from exc
    return require_mapping(
        strict_json_loads(statement, "attestation statement"),
        "attestation statement",
    )


def _subject_names_manifest(subject: dict[str, object], manifest_sha256: str) -> bool:
    """Return whether one subject record names the manifest's exact bytes."""
    digest = subject.get("digest")
    if not isinstance(digest, dict):
        return False
    if typ.cast("dict[str, object]", digest).get("sha256") != manifest_sha256:
        return False
    name = subject.get("name")
    if not isinstance(name, str):
        return False
    return PurePosixPath(name).name == MANIFEST_NAME


def _require_attestation(directory: Path, manifest_sha256: str) -> None:
    """Require the attestation to be a well-formed statement about the manifest.

    Signature validity under a trusted root is deliberately not asserted here;
    this check rejects an empty bundle, an unrelated statement and a statement
    about some other digest, which is what a publication observation can
    establish before a clean runner exercises the full verifier.
    """
    statement = _attestation_statement(directory)
    if statement.get("_type") not in _STATEMENT_TYPES:
        msg = "attestation statement is not an in-toto statement"
        raise ProverToolError(msg)
    if statement.get("predicateType") != ATTESTATION_PREDICATE:
        msg = "attestation statement is not build provenance"
        raise ProverToolError(msg)
    subjects = [
        require_mapping(subject, "attestation subject")
        for subject in require_sequence(statement.get("subject"), "attestation subject")
    ]
    if not any(
        _subject_names_manifest(subject, manifest_sha256) for subject in subjects
    ):
        msg = "attestation does not cover the published manifest"
        raise ProverToolError(msg)


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
    _require_attestation(directory, parsed.sha256)


def verify_publication(
    directory: Path,
    names: cabc.Sequence[str],
    *,
    tag: str,
    body: str | None,
) -> bool:
    """Return whether a published release authenticates its own publication.

    The published manifest is the authority for the published bytes: its
    checksums must describe it and each payload it names, every evidence asset
    must be present, its attestation must name its digest, and its recorded
    identities must reproduce the build-input fingerprint the release body
    advertises. A verification failure is reported as an unverified
    observation rather than raised, because the policy owns what an
    unverifiable publication means.

    Parameters
    ----------
    directory : Path
        Downloaded assets of the published release.
    names : collections.abc.Sequence of str
        Asset names the release serves.
    tag : str
        Immutable tag the observation was made for.
    body : str | None
        Release body, which records the build-input fingerprint.

    Returns
    -------
    bool
        Whether every check passed.
    """
    if not set(EVIDENCE_NAMES) <= set(names):
        return False
    try:
        _verify_release(directory, tag, body)
    except ProverToolError:
        # A release whose evidence does not verify is simply not verified; the
        # caller decides whether that is a conflict or a retry.
        return False
    except OSError:
        # An unreadable release is equally unverified. The clauses stay
        # separate because the project's pylint runs under Python 3.12, which
        # cannot parse the parenthesised-pair shorthand the formatter prefers.
        return False
    except ValueError:
        # Malformed JSON or base64 in an external document is unverified too.
        # `json.JSONDecodeError` and `binascii.Error` are both `ValueError`.
        return False
    return True
