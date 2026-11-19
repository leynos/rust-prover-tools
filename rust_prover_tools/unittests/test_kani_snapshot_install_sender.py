"""How each staged file is bounded and authenticated before it is read.

Two transports satisfy one contract: a reviewed local directory and the public
release the pin names. The release transport is the normal path, so these tests
pin what it asks of the downloader rather than the network: the manifest against
the reviewed digest, a payload against its manifest record, and the two
self-authenticating evidence files against a size ceiling alone. They also pin
that a local override really selects the local transport, because a pin that
silently ignored `--asset-dir` would fetch from the internet during an offline
reproduction.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.downloads import BoundedAsset, PinnedAsset
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.kani_snapshot import install_sender
from rust_prover_tools.kani_snapshot.download import (
    DownloadSettings,
    ReleaseUrlPolicy,
)
from rust_prover_tools.kani_snapshot.install_model import (
    MANIFEST_ASSET_NAME,
    MANIFEST_MAX_BYTES,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.install_sender import (
    INVENTORY_MAX_BYTES,
    PAYLOAD_MAX_BYTES,
    LocalSender,
    ReleaseSender,
    sender_for,
)
from rust_prover_tools.kani_snapshot.models import AssetRecord
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    from pathlib import Path

PIN = make_pin()
CHECKSUM = "9" * 64
PAYLOAD = AssetRecord(
    role="backend",
    name="kani-0.66.0-backend-x86_64-unknown-linux-gnu.tar.gz",
    media_type="application/gzip",
    size=4096,
    sha256=CHECKSUM,
)
INVENTORY = AssetRecord(
    role="inventory",
    name="inventory.json",
    media_type="application/json",
    size=2048,
    sha256=CHECKSUM,
)


class RecordingDownloads:
    """Record what the release transport asked to download, then fail."""

    def __init__(self) -> None:
        """Start with no recorded requests."""
        self.assets: list[PinnedAsset | BoundedAsset] = []

    def asset(
        self,
        asset: PinnedAsset,
        destination: Path,
        policy: ReleaseUrlPolicy,
        settings: DownloadSettings,
    ) -> Path:
        """Record a digest-pinned download."""
        del settings
        assert asset.name == policy.asset_name
        self.assets.append(asset)
        return destination

    def bounded(
        self,
        asset: BoundedAsset,
        destination: Path,
        limits: object,
        *,
        url_policy: ReleaseUrlPolicy,
    ) -> str:
        """Record a size-bounded download."""
        del limits
        assert asset.name == url_policy.asset_name
        self.assets.append(asset)
        return CHECKSUM


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> RecordingDownloads:
    """Replace the network with a recorder for the release transport."""
    recorder = RecordingDownloads()
    monkeypatch.setattr(install_sender, "download_asset", recorder.asset)
    monkeypatch.setattr(install_sender, "download_unverified", recorder.bounded)
    return recorder


def test_a_local_override_selects_the_local_transport(tmp_path: Path) -> None:
    """An offline reproduction must not fall back to the network."""
    sender = sender_for(InstallOptions(pin=PIN, assets_dir=tmp_path))

    assert isinstance(sender, LocalSender)


def test_a_pin_without_an_override_selects_the_release_transport() -> None:
    """The documented default is the public release the pin names."""
    sender = sender_for(InstallOptions(pin=PIN))

    assert isinstance(sender, ReleaseSender)


def test_the_manifest_is_pinned_to_the_reviewed_digest(
    tmp_path: Path, downloads: RecordingDownloads
) -> None:
    """The manifest is the root of trust, so its bytes are fixed in advance."""
    ReleaseSender(pin=PIN).manifest(tmp_path)

    [asset] = downloads.assets
    assert isinstance(asset, PinnedAsset)
    assert asset.name == MANIFEST_ASSET_NAME
    assert asset.sha256 == PIN.manifest_sha256
    assert asset.max_bytes == MANIFEST_MAX_BYTES
    assert asset.url.endswith(f"/{PIN.tag}/{MANIFEST_ASSET_NAME}")


def test_a_payload_is_pinned_to_its_manifest_record(
    tmp_path: Path, downloads: RecordingDownloads
) -> None:
    """The manifest decides the payload digest; the pin does not."""
    ReleaseSender(pin=PIN).payload(PAYLOAD, tmp_path)

    [asset] = downloads.assets
    assert isinstance(asset, PinnedAsset)
    assert asset.sha256 == PAYLOAD.sha256
    assert asset.max_bytes == min(PAYLOAD.size, PAYLOAD_MAX_BYTES)
    assert asset.url.endswith(f"/{PIN.tag}/{PAYLOAD.name}")


def test_the_inventory_uses_its_own_ceiling(
    tmp_path: Path, downloads: RecordingDownloads
) -> None:
    """A small JSON document must not be allowed to claim a payload's budget."""
    ReleaseSender(pin=PIN).payload(INVENTORY, tmp_path)

    [asset] = downloads.assets
    assert asset.max_bytes == min(INVENTORY.size, INVENTORY_MAX_BYTES)
    assert INVENTORY_MAX_BYTES < PAYLOAD_MAX_BYTES


def test_an_evidence_file_is_bounded_but_not_pinned(
    tmp_path: Path, downloads: RecordingDownloads
) -> None:
    """Checksums and attestations authenticate themselves after download."""
    ReleaseSender(pin=PIN).fixed("SHA256SUMS", 1024, tmp_path)

    [asset] = downloads.assets
    assert isinstance(asset, BoundedAsset)
    assert asset.max_bytes == 1024


def test_a_transient_failure_is_retried_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flaky response must not abort a release that is otherwise sound."""
    attempts: list[int] = []

    def flaky(
        asset: BoundedAsset,
        destination: Path,
        limits: object,
        *,
        url_policy: ReleaseUrlPolicy,
    ) -> str:
        """Fail the first attempt and answer the second."""
        del asset, destination, limits, url_policy
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            msg = "connection reset"
            raise TransientDownloadError(msg)
        return CHECKSUM

    monkeypatch.setattr(install_sender, "download_unverified", flaky)
    settings = DownloadSettings(sleep=lambda _delay: None)

    ReleaseSender(pin=PIN, settings=settings).fixed("SHA256SUMS", 1024, tmp_path)

    assert len(attempts) == 2, "the transient failure was not retried"


def test_a_local_source_must_exist_before_staging(tmp_path: Path) -> None:
    """A mistyped directory fails at the check, not after the lock is taken."""
    sender = LocalSender(source=tmp_path / "missing")

    with pytest.raises(ProverToolError, match="does not exist"):
        sender.check_source()


def test_the_release_transport_requires_a_grammatical_tag() -> None:
    """An unvalidated URL is never formed from a free-form tag."""
    pin = make_pin(tag="not-a-snapshot-tag")

    with pytest.raises(ProverToolError):
        ReleaseSender(pin=pin).check_source()
