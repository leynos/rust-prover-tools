"""How release files reach the private staging tree.

Two transports satisfy one contract. A reviewed local directory copies files
under the same size envelopes as before, which keeps offline reproduction and
the test suite possible. The public GitHub release the pin names is the normal
path: the manifest is downloaded against the pin's digest, the payloads against
the digests the manifest records, and the two self-authenticating evidence
files under size ceilings only — a checksum list is verified against the
manifest it describes and an attestation is verified cryptographically, so
neither can be pinned in advance.

Nothing here decides whether a downloaded release is trustworthy. It bounds
and verifies bytes; `install_transport` owns the order in which the manifest,
checksums, attestation and payloads may be trusted.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from rust_prover_tools.downloads import (
    BoundedAsset,
    PinnedAsset,
    download_unverified,
)
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.kani_snapshot.download import (
    DEFAULT_DOWNLOAD_SETTINGS,
    DownloadSettings,
    ReleaseUrlPolicy,
    download_asset,
)
from rust_prover_tools.kani_snapshot.install_copy import (
    TransportFile,
    TransportLimit,
    copy_local,
    transport_file,
)
from rust_prover_tools.kani_snapshot.install_model import (
    MANIFEST_ASSET_NAME,
    MANIFEST_MAX_BYTES,
    InstallOptions,
)
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import AssetRecord, SnapshotPin
    from rust_prover_tools.kani_snapshot.progress import Progress

__all__ = (
    "INVENTORY_MAX_BYTES",
    "PAYLOAD_MAX_BYTES",
    "LocalSender",
    "ReleaseSender",
    "Sender",
    "sender_for",
)

_LOGGER = logging.getLogger(__name__)

#: Design envelopes for one compressed payload and for the inventory document.
PAYLOAD_MAX_BYTES: typ.Final[int] = 2 * 1024 * 1024 * 1024
INVENTORY_MAX_BYTES: typ.Final[int] = 64 * 1024 * 1024


class Sender(typ.Protocol):
    """The transport operations staging needs, from either source."""

    def check_source(self) -> None:
        """Raise unless the source can supply files at all."""
        ...

    def manifest(self, staging: Path, checkpoint: Progress | None = None) -> Path:
        """Stage the manifest, authenticating it against the reviewed pin."""
        ...

    def fixed(
        self,
        name: str,
        max_bytes: int,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Stage one fixed-name evidence file under a size ceiling."""
        ...

    def payload(
        self,
        asset: AssetRecord,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Stage one manifest payload, authenticating it against its record."""
        ...


def _envelope(asset: AssetRecord) -> int:
    """Return the design ceiling for one payload's role."""
    return INVENTORY_MAX_BYTES if asset.role == "inventory" else PAYLOAD_MAX_BYTES


@dc.dataclass(frozen=True, slots=True)
class LocalSender:
    """Stage release files from a reviewed local directory."""

    source: Path

    def check_source(self) -> None:
        """Require the directory to exist before any copy is attempted."""
        if not self.source.is_dir():
            msg = f"snapshot asset directory does not exist: {self.source}"
            raise ProverToolError(msg)

    def manifest(self, staging: Path, checkpoint: Progress | None = None) -> Path:
        """Copy the manifest under its fixed envelope."""
        return self.fixed(
            MANIFEST_ASSET_NAME,
            MANIFEST_MAX_BYTES,
            staging,
            checkpoint,
        )

    def fixed(
        self,
        name: str,
        max_bytes: int,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Copy one fixed-name evidence file."""
        return copy_local(
            self.source,
            staging,
            transport_file(name, max_bytes),
            checkpoint,
        )

    def payload(
        self,
        asset: AssetRecord,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Copy one payload under its envelope and exact declared size."""
        limit = TransportLimit(
            min(asset.size, _envelope(asset)),
            exact_size=asset.size,
        )
        return copy_local(
            self.source,
            staging,
            TransportFile(name=asset.name, limit=limit),
            checkpoint,
        )


@dc.dataclass(frozen=True, slots=True)
class ReleaseSender:
    """Stage release files from the public release a reviewed pin names."""

    pin: SnapshotPin
    settings: DownloadSettings = DEFAULT_DOWNLOAD_SETTINGS

    def _policy(self, name: str) -> ReleaseUrlPolicy:
        """Return the exact release URL policy for one asset."""
        return ReleaseUrlPolicy.for_pin(self.pin, name)

    def check_source(self) -> None:
        """Require the pin to name a release URL this transport may use."""
        self._policy(MANIFEST_ASSET_NAME)

    def manifest(self, staging: Path, checkpoint: Progress | None = None) -> Path:
        """Download the manifest and require the reviewed digest."""
        del checkpoint  # the download is bounded by its own retry policy
        policy = self._policy(MANIFEST_ASSET_NAME)
        asset = PinnedAsset(
            name=MANIFEST_ASSET_NAME,
            url=policy.asset_url(),
            sha256=self.pin.manifest_sha256,
            max_bytes=MANIFEST_MAX_BYTES,
        )
        return download_asset(
            asset,
            staging / MANIFEST_ASSET_NAME,
            policy,
            self.settings,
        )

    def fixed(
        self,
        name: str,
        max_bytes: int,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Download one evidence file under its ceiling, unverified.

        The transport bounds the size but compares the bytes with no pinned
        digest, so authentication stays the caller's job: the install checks
        this file against the manifest and attestation before using it.
        """
        del checkpoint  # the download is bounded by its own retry policy
        policy = self._policy(name)
        destination = staging / name
        _download_with_retry(
            BoundedAsset(name=name, url=policy.asset_url(), max_bytes=max_bytes),
            destination,
            policy,
            self.settings,
        )
        return destination

    def payload(
        self,
        asset: AssetRecord,
        staging: Path,
        checkpoint: Progress | None = None,
    ) -> Path:
        """Download one payload and require its recorded size and digest."""
        del checkpoint  # the download is bounded by its own retry policy
        policy = self._policy(asset.name)
        pinned = PinnedAsset(
            name=asset.name,
            url=policy.asset_url(),
            sha256=asset.sha256,
            max_bytes=min(asset.size, _envelope(asset)),
        )
        return download_asset(pinned, staging / asset.name, policy, self.settings)


def _download_with_retry(
    asset: BoundedAsset,
    destination: Path,
    policy: ReleaseUrlPolicy,
    settings: DownloadSettings,
) -> str:
    """Download a bounded asset, retrying only transient transport failures."""
    for attempt in range(1, settings.retry.attempts + 1):
        try:
            return download_unverified(
                asset,
                destination,
                settings.limits,
                url_policy=policy,
            )
        except TransientDownloadError:
            if attempt == settings.retry.attempts:
                raise
            delay = settings.retry.delay(attempt)
            log_event(
                _LOGGER,
                "download.retry",
                attempt=attempt,
                delay_seconds=delay,
            )
            settings.sleep(delay)
    msg = "download retry loop ended without a result"
    raise AssertionError(msg)


def sender_for(options: InstallOptions) -> Sender:
    """Return the transport a reviewed pin or a local override selects."""
    if options.assets_dir is not None:
        return LocalSender(source=options.assets_dir)
    return ReleaseSender(pin=options.pin)
