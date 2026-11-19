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
    from rust_prover_tools.kani_snapshot.models import AssetRecord, SnapshotManifest

__all__ = (
    "ATTESTATION_NAME",
    "CHECKSUMS_NAME",
    "EVIDENCE_NAMES",
    "PERFORMED_CHECKS",
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


def _payload_assets(manifest: SnapshotManifest) -> list[ReleaseAsset]:
    """Return the payload assets the manifest measured."""
    return [
        ReleaseAsset(name=asset.name, size=asset.size, sha256=asset.sha256)
        for asset in manifest.assets
    ]


def _require_regular_file(name: str, path: Path, *, what: str) -> None:
    """Require `path` to be a regular file, naming it as `what`."""
    if not path.is_file():
        msg = f"{what} {name!r} is not a regular file"
        raise ProverToolError(msg)


def _stat_size(path: Path, *, what: str) -> int:
    """Return the size of `path`, reporting an unreadable file as an error."""
    try:
        return path.stat().st_size
    except OSError as exc:
        msg = f"failed to stat {what} '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _evidence_asset(name: str, directory: Path) -> ReleaseAsset:
    """Return one evidence asset measured from the release directory."""
    path = directory / name
    _require_regular_file(name, path, what="release evidence")
    size = _stat_size(path, what="release evidence")
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


def _require_payload(asset: AssetRecord, payloads: Path) -> None:
    """Require one payload file to hold exactly the bytes the manifest measured."""
    path = payloads / asset.name
    _require_regular_file(asset.name, path, what="validated payload")
    if _stat_size(path, what="payload") != asset.size:
        msg = f"payload {asset.name!r} size does not match the manifest"
        raise ProverToolError(msg)
    if sha256_file(path) != asset.sha256:
        msg = f"payload {asset.name!r} digest does not match the manifest"
        raise ProverToolError(msg)


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
        _require_payload(asset, payloads)


def _malformed_row(number: int) -> typ.NoReturn:
    """Refuse one checksum row that is not `<digest>  <name>`."""
    msg = f"checksum row {number} is not '<digest>  <name>'"
    raise ProverToolError(msg)


def _checksum_row(number: int, line: str) -> tuple[str, str]:
    """Split one `<digest>  <name>` row, refusing a malformed one."""
    digest, separator, name = line.partition("  ")
    if not separator:
        _malformed_row(number)
    if not digest or not name:
        _malformed_row(number)
    return name, digest


def _checksum_rows(data: bytes) -> dict[str, str]:
    """Parse `SHA256SUMS` into a name-to-digest mapping."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        msg = f"checksums are not ASCII: {exc}"
        raise ProverToolError(msg) from exc
    rows: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        name, digest = _checksum_row(number, line)
        if name in rows:
            msg = f"checksum row {number} repeats {name!r}"
            raise ProverToolError(msg)
        rows[name] = digest
    return rows


def _require_manifest_bytes(parsed: ParsedManifest, directory: Path) -> None:
    """Require the manifest on disk to be the exact bytes that were parsed."""
    manifest = directory / MANIFEST_NAME
    _require_regular_file(MANIFEST_NAME, manifest, what="release evidence")
    if sha256_file(manifest) != parsed.sha256:
        msg = "manifest bytes on disk differ from the parsed manifest"
        raise ProverToolError(msg)


def _require_nonempty_evidence(name: str, directory: Path) -> None:
    """Require one evidence asset to be present and non-empty."""
    path = directory / name
    _require_regular_file(name, path, what="release evidence")
    if _stat_size(path, what="release evidence") <= 0:
        msg = f"release evidence {name!r} is empty"
        raise ProverToolError(msg)


def verify_checksums(parsed: ParsedManifest, directory: Path) -> None:
    """Require the checksum document to describe exactly this release.

    Parameters
    ----------
    parsed : ParsedManifest
        Parsed manifest whose payload digests and exact bytes the checksum
        document must reproduce.
    directory : Path
        Directory holding `SHA256SUMS`, the payloads and `manifest.json`.

    Raises
    ------
    ProverToolError
        Raised when `SHA256SUMS` is missing or is not a regular file, is not
        ASCII, has a malformed or repeated row, or names a set of assets or
        digests other than exactly the manifest's payloads and manifest.
    """
    checksums = directory / CHECKSUMS_NAME
    _require_regular_file(CHECKSUMS_NAME, checksums, what="release evidence")
    rows = _checksum_rows(checksums.read_bytes())
    expected = {asset.name: asset.sha256 for asset in parsed.manifest.assets}
    expected[MANIFEST_NAME] = parsed.sha256
    if rows != expected:
        msg = "checksums do not describe exactly the payloads and manifest"
        raise ProverToolError(msg)


def verify_release(parsed: ParsedManifest, directory: Path) -> tuple[str, ...]:
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

    Returns
    -------
    tuple[str, ...]
        The checks this call performed, in the order a receipt records them.
        `PERFORMED_CHECKS` is the same tuple, exposed so callers such as the
        validate operation can name the work without repeating the labels.

    Raises
    ------
    ProverToolError
        Raised when a payload, evidence asset or checksum row disagrees.
    """
    verify_payloads(parsed, directory)
    _require_manifest_bytes(parsed, directory)
    verify_checksums(parsed, directory)
    _require_nonempty_evidence(ATTESTATION_NAME, directory)
    return PERFORMED_CHECKS
