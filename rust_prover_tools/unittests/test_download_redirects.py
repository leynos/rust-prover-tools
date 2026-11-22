"""Regression tests for redirect following and HTTP status reporting."""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

import pytest

from rust_prover_tools import download_stream, download_transport
from rust_prover_tools.downloads import DownloadLimits
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    NO_DEADLINE,
    FakeConnection,
    FakeResponse,
    download_request,
    events_from,
    stream_limits,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


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


@dc.dataclass(frozen=True, slots=True)
class _StatusCase:
    """One non-success status and how the transport must classify it."""

    status: int
    status_class: str
    is_transient: bool


@pytest.mark.parametrize(
    "case",
    [
        _StatusCase(503, "5xx", is_transient=True),
        _StatusCase(404, "4xx", is_transient=False),
    ],
    ids=["server-error", "client-error"],
)
def test_download_reports_an_http_status_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _StatusCase,
) -> None:
    """A non-success response reports its status and whether it is transient."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connection = FakeConnection()
    response = FakeResponse(status=case.status)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match=f"HTTP status {case.status}") as raised:
        download_transport._download(
            download_request(
                "https://example.test/asset.bin",
                tmp_path / "asset.bin.download",
                max_bytes=100,
                asset_name="asset.bin",
            ),
            DownloadLimits(),
        )

    assert isinstance(raised.value, TransientDownloadError) is case.is_transient, (
        "only a retryable status may be classified as transient"
    )
    assert {
        "event": "download.failed",
        "reason": "http_status",
        "status": case.status,
        "status_class": case.status_class,
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
