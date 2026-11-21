"""Regression tests for bounded, verified HTTPS downloads."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import typing as typ

import pytest

from rust_prover_tools import download_stream, download_transport, downloads
from rust_prover_tools.downloads import (
    DownloadLimits,
    PinnedAsset,
    download_pinned,
    sha256_file,
)
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    NO_DEADLINE,
    FakeClock,
    FakeConnection,
    FakeResponse,
    download_request,
    events_from,
    stream_limits,
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


def test_stream_rejects_oversized_content_length_before_reading(
    tmp_path: Path,
) -> None:
    """A declared oversized body is refused before the destination opens."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "100"},
        chunks=(b"x" * 100,),
    )

    with pytest.raises(ProverToolError, match="Content-Length 100 exceeds"):
        download_stream._stream(
            response,
            destination,
            stream_limits(10, NO_DEADLINE),
        )

    assert response.read_count == 0, "the body must not be read"
    assert not destination.exists(), "no partial file should be created"


def test_stream_stops_at_byte_ceiling_without_length_header(
    tmp_path: Path,
) -> None:
    """A streamed response without Content-Length is counted and stopped."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"123456", b"7890"))

    with pytest.raises(ProverToolError, match="streamed size exceeded"):
        download_stream._stream(
            response,
            destination,
            stream_limits(8, NO_DEADLINE),
        )

    assert response.read_count == 2, "the offending chunk should stop the read"
    assert destination.read_bytes() == b"123456", (
        "the destination must not receive the offending chunk"
    )


def test_stream_emits_a_size_limit_event_at_debug_level(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The size-limit boundary is observable when the logger opts in."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"123456", b"7890"))

    with pytest.raises(ProverToolError, match="streamed size exceeded"):
        download_stream._stream(
            response,
            destination,
            stream_limits(8, NO_DEADLINE),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert any(
        event["event"] == "download.failed" and event["reason"] == "size_limit"
        for event in events
    ), "a rejected stream should report its limit category"


def test_stream_is_silent_without_debug_logging(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default configuration produces no download events."""
    caplog.set_level(logging.WARNING, logger="rust_prover_tools.download_stream")
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"payload",))

    download_stream._stream(
        response,
        destination,
        stream_limits(100, NO_DEADLINE),
    )

    assert not caplog.records, "download events must be opt-in"


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


def test_stream_accepts_a_complete_body_matching_content_length(
    tmp_path: Path,
) -> None:
    """A body within both limits and matching its length is written."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "6"},
        chunks=(b"abc", b"def"),
    )

    download_stream._stream(response, destination, stream_limits(10, NO_DEADLINE))

    assert destination.read_bytes() == b"abcdef", "the complete body must be written"


def test_stream_rejects_an_incomplete_body(tmp_path: Path) -> None:
    """A body shorter than Content-Length is treated as a failed transfer."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "10"},
        chunks=(b"abc",),
    )

    with pytest.raises(ProverToolError, match="incomplete download"):
        download_stream._stream(
            response,
            destination,
            stream_limits(20, NO_DEADLINE),
        )


@pytest.mark.parametrize("value", ["not-a-number", "-1"])
def test_stream_rejects_invalid_content_length(
    tmp_path: Path,
    value: str,
) -> None:
    """Malformed length headers cannot bypass the ceiling."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(headers={"Content-Length": value})

    with pytest.raises(ProverToolError, match="Content-Length"):
        download_stream._stream(
            response,
            destination,
            stream_limits(10, NO_DEADLINE),
        )


def test_download_follows_a_bounded_https_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to another HTTPS host is followed and closed."""
    first_connection = FakeConnection()
    second_connection = FakeConnection()
    responses = iter(
        (
            (
                FakeResponse(
                    status=302,
                    headers={"Location": "https://cdn.example.test/asset"},
                ),
                first_connection,
            ),
            (
                FakeResponse(chunks=(b"payload",)),
                second_connection,
            ),
        ),
    )

    def fake_open(
        url: str,
        limits: DownloadLimits,
        deadline: float,
    ) -> tuple[FakeResponse, FakeConnection]:
        """Return the next scripted response and its connection."""
        del url, limits, deadline
        return next(responses)

    monkeypatch.setattr(download_transport, "_open", fake_open)
    destination = tmp_path / "asset.bin"

    download_transport._download(
        download_request("https://example.test/asset", destination, max_bytes=100),
        DownloadLimits(max_redirects=1),
    )

    assert destination.read_bytes() == b"payload", (
        "the final body must be written to the destination"
    )
    assert first_connection.closed, "the redirect connection must close"
    assert second_connection.closed, "the final connection must close"


def test_download_rejects_excess_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect ceiling stops a redirect loop."""
    connection = FakeConnection()
    response = FakeResponse(
        status=302,
        headers={"Location": "https://example.test/again"},
    )

    def fake_open(
        url: str,
        limits: DownloadLimits,
        deadline: float,
    ) -> tuple[FakeResponse, FakeConnection]:
        """Return the redirecting response and its connection."""
        del url, limits, deadline
        return response, connection

    monkeypatch.setattr(download_transport, "_open", fake_open)

    with pytest.raises(ProverToolError, match="too many redirects"):
        download_transport._download(
            download_request(
                "https://example.test/asset",
                tmp_path / "asset.bin",
                max_bytes=100,
            ),
            DownloadLimits(max_redirects=0),
        )

    assert connection.closed, "the rejected redirect connection must close"


@pytest.mark.parametrize(
    ("status", "headers", "match"),
    [
        (302, {"Location": "http://example.test/asset"}, "must use https"),
        (302, None, "no Location"),
        (404, None, "HTTP status 404"),
    ],
    ids=["downgrade", "missing-location", "non-success"],
)
def test_download_rejects_invalid_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str] | None,
    match: str,
) -> None:
    """A failed response is reported and its connection is closed."""
    connection = FakeConnection()
    response = FakeResponse(status=status, headers=headers)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match=match):
        download_transport._download(
            download_request(
                "https://example.test/asset",
                tmp_path / "asset.bin",
                max_bytes=100,
            ),
            DownloadLimits(),
        )

    assert connection.closed, "the failed connection must close"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/asset",
        "https://user:secret@example.test/asset",
        "https:///asset",
        "https://example.test:not-a-port/asset",
    ],
)
def test_download_rejects_urls_outside_the_https_policy(url: str) -> None:
    """Only credential-free HTTPS URLs with a valid host are accepted."""
    with pytest.raises(ProverToolError):
        download_transport._validate_url(url)


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


def test_stream_reports_a_write_failure(tmp_path: Path) -> None:
    """A filesystem error while writing the body is a storage failure.

    Opening or writing the destination cannot be repaired by asking the same
    server again, so the error must not be classified as a transient download.
    """
    destination = tmp_path / "asset.bin"
    destination.mkdir()
    response = FakeResponse(chunks=(b"payload",))

    with pytest.raises(ProverToolError, match="failed to store"):
        download_stream._stream(
            response,
            destination,
            stream_limits(100, NO_DEADLINE),
        )


def test_download_complete_event_reports_bytes_and_elapsed_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Completion reports the transferred size and duration, not the asset."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    payload = b"verified"
    response = FakeResponse(chunks=(payload,))
    connection = FakeConnection()
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    download_transport._download(
        download_request(
            "https://example.test/asset.bin",
            tmp_path / "asset.bin.download",
            max_bytes=100,
            asset_name="asset.bin",
        ),
        DownloadLimits(),
    )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    complete = [event for event in events if event["event"] == "download.complete"]
    assert len(complete) == 1, "a successful download emits one completion event"
    event = complete[0]
    assert "asset" not in event, "completion must not carry an asset-name label"
    assert event["written"] == len(payload), "completion reports the body bytes"
    assert event["redirects"] == 0, "a direct download followed no redirects"
    elapsed = event["elapsed_seconds"]
    assert isinstance(elapsed, float), "completion reports a float elapsed time"
    assert elapsed >= 0.0, "completion reports a non-negative elapsed time"
    labelled = [
        event
        for event in events
        if str(event["event"]).startswith("download.") and "asset" in event
    ]
    assert labelled == [], "no download event may carry an asset-name label"


def test_download_reports_a_missing_redirect_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A redirect without a Location header reports that bounded reason."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connection = FakeConnection()
    response = FakeResponse(status=302)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match="no Location"):
        download_transport._download(
            download_request(
                "https://example.test/asset.bin",
                tmp_path / "asset.bin.download",
                max_bytes=100,
                asset_name="asset.bin",
            ),
            DownloadLimits(),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {
        "event": "download.failed",
        "reason": "redirect_location",
        "status": 302,
    } in events, "a bad redirect should report its status and bounded reason"


@pytest.mark.parametrize(
    ("status", "status_class", "is_transient"),
    [(503, "5xx", True), (404, "4xx", False)],
)
def test_download_reports_an_http_status_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    status_class: str,
    *,
    is_transient: bool,
) -> None:
    """A non-success response reports its status and whether it is transient."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connection = FakeConnection()
    response = FakeResponse(status=status)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match=f"HTTP status {status}") as raised:
        download_transport._download(
            download_request(
                "https://example.test/asset.bin",
                tmp_path / "asset.bin.download",
                max_bytes=100,
                asset_name="asset.bin",
            ),
            DownloadLimits(),
        )

    assert isinstance(raised.value, TransientDownloadError) is is_transient, (
        "only a retryable status may be classified as transient"
    )
    assert {
        "event": "download.failed",
        "reason": "http_status",
        "status": status,
        "status_class": status_class,
    } in events_from(caplog), "a failed response should report a bounded status class"


def test_download_reports_a_stream_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error while reading the body is reported once."""

    class FailingResponse(FakeResponse):
        """A response whose body read fails with a connection reset."""

        def read(self, amt: int | None = None) -> bytes:
            """Fail every body read with a connection reset."""
            del amt
            msg = "connection reset"
            raise OSError(msg)

    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")

    with pytest.raises(TransientDownloadError, match=r"failed to download asset\.bin"):
        download_stream._stream(
            FailingResponse(),
            tmp_path / "asset.bin",
            stream_limits(100, NO_DEADLINE),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {
        "event": "download.failed",
        "reason": "stream_error",
    } in events, "a body transport error should report one bounded event"


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
