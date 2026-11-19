"""Download and authenticate the bytes one release currently serves.

`observe` reports what the API says a release holds; this module answers the
separate question of what it actually serves. The digests that authorise a
publication are computed here from downloaded bytes, and the release's own
manifest, checksums and attestation are authenticated against each other, so a
claimed digest can never stand in for evidence.

Verification is a download, so it is the one release operation that can be
expected to take a transfer budget. A release whose bytes cannot be fetched
raises `ReleaseTransientError` rather than reporting an unverifiable
publication: a retry may succeed, and treating it as a conflict would refuse a
release that is in fact intact.
"""

from __future__ import annotations

import logging
import pathlib
import tempfile
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_document import verify_publication
from rust_prover_tools.kani_snapshot.gh_transport import (
    TRANSFER_TIMEOUT_SECONDS,
    gh,
)
from rust_prover_tools.kani_snapshot.release_model import (
    ObservedRelease,
    VerifiedAssets,
)
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("verify_assets",)

_LOGGER = logging.getLogger(__name__)


def verify_assets(
    repository: str,
    tag: str,
    observed: ObservedRelease,
    names: cabc.Sequence[str],
) -> VerifiedAssets:
    """Download the release's assets, hash them and authenticate its evidence.

    Every asset the observation reports is downloaded, not only the names
    the caller asked about, because the manifest, checksums and attestation
    can only authenticate each other as a complete set. The returned
    digests therefore cover the whole release, and the names argument is a
    guard that the caller is asking about assets the release still reports.

    Verification is a download, so a release whose bytes cannot be fetched
    raises `ReleaseTransientError` rather than reporting an unverifiable
    publication: a retry may succeed, and treating it as a conflict would
    refuse a release that is in fact intact.
    """
    requested = tuple(names)
    if not requested:
        msg = "verification must name at least one asset"
        raise ProverToolError(msg)
    available = {asset.name for asset in observed.assets}
    unknown = sorted(set(requested) - available)
    if unknown:
        msg = f"cannot verify assets the release does not report: {', '.join(unknown)}"
        raise ProverToolError(msg)
    downloaded = tuple(asset.name for asset in observed.assets)
    with tempfile.TemporaryDirectory(prefix="kani-publish-") as name:
        directory = pathlib.Path(name)
        _download(repository, tag, downloaded, directory)
        digests = tuple((asset, sha256_file(directory / asset)) for asset in downloaded)
        reason = verify_publication(
            directory,
            downloaded,
            tag=tag,
            body=observed.body,
        )
    # The verdict is a boolean and the count is bounded; the reason text
    # can embed a path or a server message, so it stays out of the event.
    log_event(
        _LOGGER,
        "kani.snapshot.verify.complete",
        assets=len(digests),
        verified=reason is None,
    )
    return VerifiedAssets(
        digests=digests,
        provenance_verified=reason is None,
        reason=reason,
    )


def _download(
    repository: str,
    tag: str,
    names: cabc.Sequence[str],
    directory: Path,
) -> None:
    """Download the named assets into one directory."""
    for name in names:
        gh(
            (
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--pattern",
                name,
                "--dir",
                str(directory),
            ),
            timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        )
