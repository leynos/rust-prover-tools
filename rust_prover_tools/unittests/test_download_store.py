"""Regression tests for digest pinning and the atomic verified store."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import typing as typ

import pytest

from rust_prover_tools import download_transport, downloads
from rust_prover_tools.downloads import (
    BoundedAsset,
    DownloadLimits,
    PinnedAsset,
    download_pinned,
    download_unverified,
    sha256_file,
)
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.unittests.download_support import (
    FakeClock,
    FakeConnection,
    FakeResponse,
    events_from,
)

if typ.TYPE_CHECKING:
    import os
    from pathlib import Path


def deny_pending_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make removing a pending download fail as an unwritable directory would."""
    original_unlink = pathlib.Path.unlink

    def deny_unlink(self: pathlib.Path, *, missing_ok: bool = False) -> None:
        """Raise a permission error when a pending download is unlinked."""
        if self.name.endswith(".download"):
            raise PermissionError(13, "permission denied", str(self))
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", deny_unlink)


def test_download_pinned_emits_a_cache_hit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cache hit is observable when the logger opts in."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    payload = b"cached"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(payload)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=sha256_file(destination),
        max_bytes=100,
    )

    download_pinned(asset, destination)

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {"event": "download.cache_hit"} in events, (
        "a cache hit must not carry an asset-name label"
    )


def _bounded_asset(max_bytes: int = 100) -> BoundedAsset:
    """Return one unverified asset bounded only by its size."""
    return BoundedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        max_bytes=max_bytes,
    )


def _refuse_network(request: object, limits: object) -> None:
    """Fail the test if a reused file falls back to the network."""
    del request, limits
    msg = "network must not be used for a reused file"
    raise AssertionError(msg)


def test_download_unverified_reuses_an_existing_file_that_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file already within the ceiling is hashed in place, not refetched.

    The digest describes whatever the file holds: this function observes the
    bytes and makes no claim about them.
    """
    payload = b"already here"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(payload)
    monkeypatch.setattr(downloads, "_download", _refuse_network)

    digest = download_unverified(_bounded_asset(), destination)

    assert digest == hashlib.sha256(payload).hexdigest(), (
        "the reported digest must describe the reused file"
    )
    assert destination.read_bytes() == payload


def test_download_unverified_replaces_an_oversized_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file past the ceiling is not trusted, so it is downloaded again."""
    payload = b"fresh payload"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(b"x" * 500)

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: float,
    ) -> None:
        """Write the served payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)

    digest = download_unverified(_bounded_asset(), destination)

    assert digest == hashlib.sha256(payload).hexdigest(), (
        "the digest must describe the bytes just downloaded"
    )
    assert destination.read_bytes() == payload, (
        "the oversized file must be replaced by the downloaded payload"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "the pending file must be renamed, not left behind"
    )


def test_download_unverified_downloads_when_nothing_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent destination is downloaded and published atomically."""
    payload = b"downloaded"

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: float,
    ) -> None:
        """Write the served payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)
    destination = tmp_path / "nested" / "asset.bin"

    digest = download_unverified(_bounded_asset(), destination)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload


def test_download_pinned_reuses_a_matching_cache_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached asset with the pinned digest avoids the network."""
    payload = b"cached"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(payload)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=sha256_file(destination),
        max_bytes=100,
    )

    def fail_download(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Fail the test if a cache hit falls back to the network."""
        del request, limits
        msg = "network must not be used for a cache hit"
        raise AssertionError(msg)

    monkeypatch.setattr(downloads, "_download", fail_download)

    assert download_pinned(asset, destination) == destination, (
        "a cache hit must return the destination path"
    )
    assert destination.read_bytes() == payload, (
        "a cache hit must keep the verified bytes"
    )


def test_download_pinned_removes_pending_file_on_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest mismatch never leaves a pending or final asset behind."""
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_wrong_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write bytes whose digest cannot match the pinned asset."""
        del limits
        request.destination.write_bytes(b"wrong")

    monkeypatch.setattr(downloads, "_download", write_wrong_payload)

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_pinned(asset, destination)

    assert not destination.exists(), (
        "a digest mismatch must not publish the destination"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "a digest mismatch must remove the pending file"
    )


def test_download_pinned_reports_a_digest_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure to hash the pending file is one bounded download failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the pending payload that the digest check then fails to read."""
        del limits
        request.destination.write_bytes(b"payload")

    def fail_to_hash(path: Path) -> str:
        """Raise a bounded read error instead of hashing the pending file."""
        msg = f"failed to read '{path}': permission denied"
        raise ProverToolError(msg)

    monkeypatch.setattr(downloads, "_download", write_payload)
    monkeypatch.setattr(downloads, "sha256_file", fail_to_hash)

    with pytest.raises(ProverToolError, match="failed to read"):
        download_pinned(asset, destination)

    assert [event for event in events_from(caplog) if "failed" in str(event)] == [
        {"event": "download.failed", "reason": "digest_read"}
    ], "a digest read failure must report exactly one bounded reason"
    assert not destination.exists(), "an unverified response must not be published"
    assert not destination.with_name("asset.bin.download").exists(), (
        "a digest read failure must still remove the pending file"
    )


def test_download_pinned_reports_a_pending_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup failure after a verified store is a bounded failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)
    deny_pending_cleanup(monkeypatch)

    with pytest.raises(ProverToolError, match="failed to remove pending download"):
        download_pinned(asset, destination)

    assert [event for event in events_from(caplog) if "failed" in str(event)] == [
        {"event": "download.failed", "reason": "cleanup_error"}
    ], "a cleanup failure must report exactly one bounded reason"
    assert not any(
        event.get("event") == "download.verified" for event in events_from(caplog)
    ), "a failed cleanup must not report a completed verification"
    assert destination.read_bytes() == payload, (
        "the verified asset stays published even when cleanup fails"
    )


def test_download_pinned_keeps_the_primary_failure_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup failure must not replace the digest mismatch that caused it."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_wrong_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write bytes whose digest cannot match the pinned asset."""
        del limits
        request.destination.write_bytes(b"wrong")

    monkeypatch.setattr(downloads, "_download", write_wrong_payload)
    deny_pending_cleanup(monkeypatch)

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_pinned(asset, destination)

    events = events_from(caplog)
    assert [event for event in events if event.get("event") == "download.failed"] == [
        {"event": "download.failed", "reason": "digest_mismatch"}
    ], "the primary failure must remain the only download failure event"
    assert {
        "event": "download.cleanup_failed",
        "reason": "cleanup_error",
    } in events, "a masked cleanup failure should still be observable"


def test_download_pinned_stores_a_verified_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified download is moved from the pending path to its destination."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)

    assert download_pinned(asset, destination) == destination, (
        "a verified download must return the destination path"
    )
    assert destination.read_bytes() == payload, (
        "the published file must contain the verified payload"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "the pending file must be renamed, not left behind"
    )


def test_download_pinned_removes_pending_file_when_stream_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The content-length ceiling removes the pending file from disk."""
    clock = FakeClock()
    monkeypatch.setattr(download_transport, "monotonic", clock.monotonic)
    connection = FakeConnection()
    response = FakeResponse(
        headers={"Content-Length": "100"},
        chunks=(b"x" * 100,),
    )
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )
    destination = tmp_path / "asset.bin"

    with pytest.raises(ProverToolError, match="Content-Length"):
        download_pinned(
            PinnedAsset(
                name="asset.bin",
                url="https://example.test/asset.bin",
                sha256="0" * 64,
                max_bytes=10,
            ),
            destination,
            DownloadLimits(transfer_timeout_seconds=60.0),
        )

    assert not destination.exists(), (
        "a stopped download must not publish the destination"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "a stopped download must remove the pending file"
    )


def test_download_pinned_reports_a_directory_creation_failure(
    tmp_path: Path,
) -> None:
    """A destination whose parent cannot be created is reported."""
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory", encoding="utf-8")
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    with pytest.raises(ProverToolError, match="failed to create download directory"):
        download_pinned(asset, blocker / "asset.bin")


def test_download_pinned_reports_a_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified download that cannot be published is reported."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)
    original_replace = pathlib.Path.replace

    def deny_publish(
        path: pathlib.Path,
        target: pathlib.Path,
    ) -> pathlib.Path:
        """Raise a permission error when the pending file is published."""
        if path.name == "asset.bin.download":
            raise PermissionError(13, "permission denied", str(path))
        return original_replace(path, target)

    monkeypatch.setattr(pathlib.Path, "replace", deny_publish)

    with pytest.raises(ProverToolError, match="failed to store"):
        download_pinned(asset, destination)


def test_sha256_file_reports_an_unreadable_path(tmp_path: Path) -> None:
    """A path that cannot be read as a file is reported."""
    with pytest.raises(ProverToolError, match="failed to read"):
        sha256_file(tmp_path)


def test_download_pinned_passes_the_logical_asset_to_the_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The downloader reports the pinned asset name, not the pending path."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )
    captured: dict[str, str | None] = {}

    def record_asset(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Record the logical asset name and write the verified payload."""
        del limits
        captured["asset_name"] = request.asset_name
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", record_asset)

    download_pinned(asset, destination)

    assert captured["asset_name"] == "asset.bin", (
        "transport events must use the same asset value as downloader events"
    )


def test_download_pinned_reports_an_unreadable_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache inspection failure is reported before any network work."""

    class UnreadablePath:
        """A destination whose `stat` reports an environmental failure."""

        def __init__(self, path: Path) -> None:
            """Wrap the path whose metadata lookups will fail."""
            self._path = path

        @property
        def parent(self) -> Path:
            """The parent directory of the wrapped path."""
            return self._path.parent

        def stat(self) -> os.stat_result:
            """Raise a permission error for every metadata lookup."""
            raise PermissionError(13, "permission denied", str(self._path))

        def __getattr__(self, name: str) -> object:
            """Forward every other attribute lookup to the wrapped path."""
            return getattr(self._path, name)

    calls: list[tuple[object, object]] = []

    def record_download(*args: object, **kwargs: object) -> None:
        """Record the download call without performing any transfer."""
        calls.append((args, kwargs))

    monkeypatch.setattr(downloads, "_download", record_download)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )
    destination = typ.cast("Path", UnreadablePath(tmp_path / "asset.bin"))

    with pytest.raises(ProverToolError, match="failed to inspect download cache"):
        download_pinned(asset, destination)

    assert not calls, "an unreadable cache entry must not trigger a download"
