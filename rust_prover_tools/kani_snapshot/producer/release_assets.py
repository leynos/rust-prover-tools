"""The complete immutable asset set one publication must serve.

The manifest names four payloads; a release also carries the manifest, its
checksums and the build attestation. Publication verifies the whole set, so the
expected assets are derived here once, from the manifest bytes and the release
directory, rather than being listed by a workflow step that could drift.

`SHA256SUMS` is parsed strictly because it is a convenience document, not a
trust anchor: it must describe exactly the payloads and the manifest, and every
row is checked against the bytes on disk before publication trusts either.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.assemble import MANIFEST_NAME
from rust_prover_tools.kani_snapshot.release_model import ReleaseAsset

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.manifest import ParsedManifest
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest

__all__ = (
    "ATTESTATION_NAME",
    "CHECKSUMS_NAME",
    "EVIDENCE_NAMES",
    "release_asset_names",
    "release_assets",
    "verify_payloads",
    "verify_release",
)

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


def release_asset_names(manifest: SnapshotManifest) -> tuple[str, ...]:
    """Return every asset name a release of `manifest` must serve."""
    return tuple(asset.name for asset in manifest.assets) + EVIDENCE_NAMES


def _payload_assets(manifest: SnapshotManifest) -> list[ReleaseAsset]:
    """Return the payload assets the manifest measured."""
    return [
        ReleaseAsset(name=asset.name, size=asset.size, sha256=asset.sha256)
        for asset in manifest.assets
    ]


def _evidence_asset(name: str, directory: Path) -> ReleaseAsset:
    """Return one evidence asset measured from the release directory."""
    path = directory / name
    if not path.is_file():
        msg = f"release evidence {name!r} is not a regular file"
        raise ProverToolError(msg)
    try:
        size = path.stat().st_size
    except OSError as exc:
        msg = f"failed to stat release evidence '{path}': {exc}"
        raise ProverToolError(msg) from exc
    if size <= 0:
        msg = f"release evidence {name!r} is empty"
        raise ProverToolError(msg)
    return ReleaseAsset(name=name, size=size, sha256=sha256_file(path))


def release_assets(parsed: ParsedManifest, directory: Path) -> tuple[ReleaseAsset, ...]:
    """Return the complete expected asset set for one assembled release.

    Parameters
    ----------
    parsed : ParsedManifest
        Parsed manifest whose payload assets are already measured.
    directory : Path
        Directory holding the payloads, manifest, checksums and attestation.

    Returns
    -------
    tuple[ReleaseAsset, ...]
        Payloads in manifest order, then the evidence assets.

    Raises
    ------
    ProverToolError
        Raised when an evidence asset is missing, unreadable or empty.
    """
    payloads = _payload_assets(parsed.manifest)
    evidence = [_evidence_asset(name, directory) for name in EVIDENCE_NAMES]
    return (*payloads, *evidence)


def verify_payloads(parsed: ParsedManifest, payloads: Path) -> None:
    """Require every payload file to match the manifest's measured bytes.

    Parameters
    ----------
    parsed : ParsedManifest
        Parsed manifest naming the assets.
    payloads : Path
        Directory holding the payload files.

    Raises
    ------
    ProverToolError
        Raised when a payload is missing, is not a regular file, or has a size
        or digest other than the one the manifest records.
    """
    for asset in parsed.manifest.assets:
        path = payloads / asset.name
        if not path.is_file():
            msg = f"validated payload {asset.name!r} is not a regular file"
            raise ProverToolError(msg)
        try:
            size = path.stat().st_size
        except OSError as exc:
            msg = f"failed to stat payload '{path}': {exc}"
            raise ProverToolError(msg) from exc
        if size != asset.size:
            msg = f"payload {asset.name!r} size does not match the manifest"
            raise ProverToolError(msg)
        if sha256_file(path) != asset.sha256:
            msg = f"payload {asset.name!r} digest does not match the manifest"
            raise ProverToolError(msg)


def _checksum_rows(data: bytes) -> dict[str, str]:
    """Parse `SHA256SUMS` into a name-to-digest mapping."""
    rows: dict[str, str] = {}
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        msg = f"checksums are not ASCII: {exc}"
        raise ProverToolError(msg) from exc
    for number, line in enumerate(text.splitlines(), start=1):
        digest, separator, name = line.partition("  ")
        if not separator:
            msg = f"checksum row {number} is not '<digest>  <name>'"
            raise ProverToolError(msg)
        if not digest or not name:
            msg = f"checksum row {number} is not '<digest>  <name>'"
            raise ProverToolError(msg)
        if name in rows:
            msg = f"checksum row {number} repeats {name!r}"
            raise ProverToolError(msg)
        rows[name] = digest
    return rows


def verify_checksums(parsed: ParsedManifest, directory: Path) -> None:
    """Require the checksum document to describe exactly this release."""
    checksums = directory / CHECKSUMS_NAME
    if not checksums.is_file():
        msg = f"release evidence {CHECKSUMS_NAME!r} is not a regular file"
        raise ProverToolError(msg)
    rows = _checksum_rows(checksums.read_bytes())
    expected = {asset.name: asset.sha256 for asset in parsed.manifest.assets}
    expected[MANIFEST_NAME] = parsed.sha256
    if rows != expected:
        msg = "checksums do not describe exactly the payloads and manifest"
        raise ProverToolError(msg)


def verify_release(parsed: ParsedManifest, directory: Path) -> None:
    """Require the complete release set to match the manifest bytes.

    The attestation's signature is a consumer's concern; this check records
    that the exact bundle this run produced is present and non-empty, and that
    the payloads, manifest and checksums all agree before publication.

    Parameters
    ----------
    parsed : ParsedManifest
        Parsed manifest read by the validating job.
    directory : Path
        Directory holding the payloads, manifest, checksums and attestation.

    Raises
    ------
    ProverToolError
        Raised when a payload, evidence asset or checksum row disagrees.
    """
    verify_payloads(parsed, directory)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        msg = f"release evidence {MANIFEST_NAME!r} is not a regular file"
        raise ProverToolError(msg)
    if sha256_file(manifest) != parsed.sha256:
        msg = "manifest bytes on disk differ from the parsed manifest"
        raise ProverToolError(msg)
    verify_checksums(parsed, directory)
    attestation = directory / ATTESTATION_NAME
    if not attestation.is_file():
        msg = f"release evidence {ATTESTATION_NAME!r} is not a regular file"
        raise ProverToolError(msg)
    if attestation.stat().st_size <= 0:
        msg = f"release evidence {ATTESTATION_NAME!r} is empty"
        raise ProverToolError(msg)
