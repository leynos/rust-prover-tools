"""Regression tests for bounded, verified HTTPS downloads."""

from __future__ import annotations

import hashlib
import typing as typ

import pytest

from rust_prover_tools import download_transport, downloads
from rust_prover_tools.downloads import (
    DownloadLimits,
    PinnedAsset,
    download_pinned,
    sha256_file,
)
from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

# Tests that exercise byte limits rather than time keep the deadline open.
NO_DEADLINE = float("inf")


class FakeClock:
    """A deterministic monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward."""
        self.now += seconds


class FakeResponse:
    """A response object that satisfies the downloader protocol."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        clock: FakeClock | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.chunks = chunks
        self.read_count = 0
        self._clock = clock

    def getheader(
        self,
        name: str,
        default: str | None = None,
    ) -> str | None:
        """Return a canned header."""
        return self.headers.get(name, default)

    def read(self, amt: int | None = None) -> bytes:
        """Return the next canned chunk, or an empty chunk at EOF."""
        del amt
        if self._clock is not None:
            self._clock.advance(1.0)
        if self.read_count >= len(self.chunks):
            return b""
        chunk = self.chunks[self.read_count]
        self.read_count += 1
        return chunk


class FakeConnection:
    """A connection object that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        """Record closure."""
        self.closed = True


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
        download_transport._stream(
            response, destination, max_bytes=10, deadline=NO_DEADLINE
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
        download_transport._stream(
            response, destination, max_bytes=8, deadline=NO_DEADLINE
        )

    assert response.read_count == 2, "the offending chunk should stop the read"
    assert destination.read_bytes() == b"123456", (
        "the destination must not receive the offending chunk"
    )


def test_stream_stops_when_total_deadline_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow stream is stopped by the overall transfer deadline."""
    clock = FakeClock()
    monkeypatch.setattr(download_transport, "monotonic", clock.monotonic)
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"a", b"b"), clock=clock)

    with pytest.raises(ProverToolError, match="transfer deadline"):
        download_transport._stream(response, destination, max_bytes=100, deadline=0.0)

    assert response.read_count == 1, "the deadline should stop further reads"
    assert destination.read_bytes() == b"a", (
        "the bytes read before the deadline must be kept"
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

    download_transport._stream(
        response, destination, max_bytes=10, deadline=NO_DEADLINE
    )

    assert destination.read_bytes() == b"abcdef", "the complete body must be written"


def test_stream_rejects_an_incomplete_body(tmp_path: Path) -> None:
    """A body shorter than Content-Length is treated as a failed transfer."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "10"},
        chunks=(b"abc",),
    )

    with pytest.raises(ProverToolError, match="incomplete download"):
        download_transport._stream(
            response, destination, max_bytes=20, deadline=NO_DEADLINE
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
        download_transport._stream(
            response, destination, max_bytes=10, deadline=NO_DEADLINE
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
        del url, limits, deadline
        return next(responses)

    monkeypatch.setattr(download_transport, "_open", fake_open)
    destination = tmp_path / "asset.bin"

    download_transport._download(
        "https://example.test/asset",
        destination,
        max_bytes=100,
        limits=DownloadLimits(max_redirects=1),
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
        del url, limits, deadline
        return response, connection

    monkeypatch.setattr(download_transport, "_open", fake_open)

    with pytest.raises(ProverToolError, match="too many redirects"):
        download_transport._download(
            "https://example.test/asset",
            tmp_path / "asset.bin",
            max_bytes=100,
            limits=DownloadLimits(max_redirects=0),
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
            "https://example.test/asset",
            tmp_path / "asset.bin",
            max_bytes=100,
            limits=DownloadLimits(),
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
        url: str,
        target: Path,
        max_bytes: int,
        limits: DownloadLimits,
    ) -> None:
        del url, target, max_bytes, limits
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
        url: str,
        target: Path,
        max_bytes: int,
        limits: DownloadLimits,
    ) -> None:
        del url, max_bytes, limits
        target.write_bytes(b"wrong")

    monkeypatch.setattr(downloads, "_download", write_wrong_payload)

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_pinned(asset, destination)

    assert not destination.exists(), (
        "a digest mismatch must not publish the destination"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "a digest mismatch must remove the pending file"
    )


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
        url: str,
        target: Path,
        max_bytes: int,
        limits: DownloadLimits,
    ) -> None:
        del url, max_bytes, limits
        target.write_bytes(payload)

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
