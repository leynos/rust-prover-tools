"""Tests for snapshot release URL policy and bounded retrying downloads."""

from __future__ import annotations

import errno
import hashlib
import pathlib
import typing as typ

import pytest

from rust_prover_tools import download_transport
from rust_prover_tools.download_transport import DownloadLimits, TransientDownloadError
from rust_prover_tools.downloads import PinnedAsset
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.download import (
    DownloadRetryPolicy,
    DownloadSettings,
    ReleaseUrlPolicy,
    download_asset,
)
from tests.kani_snapshot_fixtures import TAG

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_ASSET = "backend.tar.gz"
_REPOSITORY = "leynos/rust-prover-tools"
_RELEASE_URL = f"https://github.com/{_REPOSITORY}/releases/download/{TAG}/{_ASSET}"


def _policy() -> ReleaseUrlPolicy:
    """Return the release policy under test."""
    return ReleaseUrlPolicy(repository=_REPOSITORY, tag=TAG, asset_name=_ASSET)


def _asset() -> PinnedAsset:
    """Return a pinned asset whose transport is replaced in retry tests."""
    return PinnedAsset(
        name=_ASSET,
        url=_RELEASE_URL,
        sha256="0" * 64,
        max_bytes=1024,
    )


def test_asset_url_is_the_exact_release_path() -> None:
    """The policy builds the only URL a snapshot asset may use."""
    assert _policy().asset_url() == _RELEASE_URL


@pytest.mark.parametrize(
    "url",
    [
        _RELEASE_URL.replace("https://", "http://"),
        _RELEASE_URL.replace("github.com", "user:secret@github.com"),
        f"{_RELEASE_URL}?download=1",
        _RELEASE_URL.replace(_REPOSITORY, "someone/else"),
        _RELEASE_URL.replace(TAG, "kani-snapshot-2026-09-bbbbbbbbbbbb-r1"),
        _RELEASE_URL.replace(_ASSET, "launcher.tar.gz"),
        f"{_RELEASE_URL}/extra",
        _RELEASE_URL.replace("github.com", "github.com.evil.test"),
        _RELEASE_URL.replace("github.com", "127.0.0.1"),
        _RELEASE_URL.replace("https://github.com", "https://github.com:8443"),
        f"{_RELEASE_URL}#fragment",
        _RELEASE_URL.replace(_ASSET, "..%2Fbackend.tar.gz"),
    ],
)
def test_initial_url_outside_the_release_policy_is_rejected(url: str) -> None:
    """Only the pinned repository, tag and asset over HTTPS are accepted."""
    with pytest.raises(ProverToolError, match="release"):
        _policy().check_initial(url)


@pytest.mark.parametrize(
    "url",
    [
        (
            f"https://release-assets.githubusercontent.com/github-production/"
            f"release-asset/{_REPOSITORY}/123/{_ASSET}?sp=r&sig=abc"
        ),
        (
            f"https://objects.githubusercontent.com/"
            f"github-production-release-asset/123/{_ASSET}?X-Amz-Signature=abc"
        ),
    ],
)
def test_documented_github_asset_redirect_hosts_are_accepted(url: str) -> None:
    """The two GitHub asset redirect hosts may carry a signed query."""
    _policy().check_redirect(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://release-assets.githubusercontent.com/asset",
        "https://user:secret@release-assets.githubusercontent.com/asset",
        "https://evil.test/github-production-release-asset/asset",
        "https://release-assets.githubusercontent.com:8443/asset",
        "https://127.0.0.1/asset",
        "https://release-assets.githubusercontent.com/asset#fragment",
        "https://release-assets.githubusercontent.com/../asset",
    ],
)
def test_redirect_outside_the_documented_hosts_is_rejected(url: str) -> None:
    """A redirect may not relax the host, scheme, port or credential policy."""
    with pytest.raises(ProverToolError, match="redirect"):
        _policy().check_redirect(url)


def test_transport_checks_every_redirect_before_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile redirect is refused without a second connection."""

    class FakeConnection:
        """A connection double that records closure."""

        def __init__(self) -> None:
            """Start open."""
            self.closed = False

        def close(self) -> None:
            """Record closure."""
            self.closed = True

    class FakeResponse:
        """A redirecting response."""

        status = 302

        def getheader(self, name: str, default: str | None = None) -> str | None:
            """Return the redirect target."""
            del default
            if name == "Location":
                return "https://evil.test/asset"
            return None

    connection = FakeConnection()
    opened: list[str] = []

    def fake_open(
        url: str,
        limits: DownloadLimits,
        deadline: float,
        *,
        asset_name: str,
    ) -> tuple[FakeResponse, FakeConnection]:
        """Record the opened URL and return the redirect."""
        del limits, deadline, asset_name
        opened.append(url)
        return FakeResponse(), connection

    monkeypatch.setattr(download_transport, "_open", fake_open)
    request = download_transport._DownloadRequest(
        _RELEASE_URL,
        tmp_path / "asset.download",
        max_bytes=1024,
        asset_name=_ASSET,
        url_policy=_policy(),
    )

    with pytest.raises(ProverToolError, match="redirect"):
        download_transport._download(request, DownloadLimits(max_redirects=5))

    assert opened == [_RELEASE_URL], "the rejected redirect must not be opened"
    assert connection.closed, "the redirecting connection must close"


class _Response:
    """A canned response that satisfies the transport's response protocol."""

    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        read_error: OSError | None = None,
    ) -> None:
        """Store the status, the body to return once, and any read failure."""
        self.status = status
        self._body = body
        self._read_error = read_error
        self._read = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        """Return the declared body length."""
        if name == "Content-Length":
            return str(len(self._body))
        return default

    def read(self, amt: int | None = None) -> bytes:
        """Return the body once, then end the stream."""
        del amt
        if self._read_error is not None:
            raise self._read_error
        if self._read:
            return b""
        self._read = True
        return self._body


class _Connection:
    """A connection double that only needs to close."""

    def close(self) -> None:
        """Close the connection."""


class _ServingConnection(_Connection):
    """A connection double that returns one canned response."""

    def __init__(self, response: _Response) -> None:
        """Store the response this connection serves."""
        self._response = response

    def request(self, method: str, url: str, *, headers: object) -> None:
        """Accept the request without touching the network."""
        del method, url, headers

    def getresponse(self) -> _Response:
        """Return the canned response."""
        return self._response


class _RefusingConnection(_Connection):
    """A connection double whose request fails like a dropped connection."""

    def request(self, method: str, url: str, *, headers: object) -> None:
        """Fail the request before any response exists."""
        del method, url, headers
        msg = "connection reset by peer"
        raise OSError(msg)

    def getresponse(self) -> _Response:
        """Fail: a refused request never produces a response."""
        msg = "getresponse must not be reached after a failed request"
        raise AssertionError(msg)


class _ScriptedConnections:
    """A connection factory that hands out scripted connections in order."""

    def __init__(self, *connections: _Connection) -> None:
        """Store the connections to hand out."""
        self._connections = list(connections)
        self.opened = 0

    def __call__(
        self,
        host: str,
        port: int | None,
        *,
        timeout: float,
    ) -> _Connection:
        """Return the next scripted connection."""
        del host, port, timeout
        self.opened += 1
        return self._connections.pop(0)


def _hashed_asset(payload: bytes) -> PinnedAsset:
    """Return a pinned asset whose digest matches `payload`."""
    return PinnedAsset(
        name=_ASSET,
        url=_RELEASE_URL,
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=1024,
    )


def test_download_asset_retries_a_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection is retried through the production transport."""
    payload = b"payload"
    connections = _ScriptedConnections(
        _RefusingConnection(),
        _ServingConnection(_Response(200, payload)),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        connections,
    )

    result = download_asset(
        _hashed_asset(payload),
        tmp_path / "asset",
        _policy(),
        settings=DownloadSettings(
            retry=DownloadRetryPolicy(attempts=2, backoff_seconds=0.5),
            sleep=sleeps.append,
        ),
    )

    assert result == tmp_path / "asset"
    assert connections.opened == 2, "a dropped connection must be retried"
    assert sleeps == [0.5], "the retry waits the configured backoff"
    assert result.read_bytes() == payload, "the retry must store the payload"


def test_download_asset_retries_a_body_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response body that fails mid-read is retried as a transport fault."""
    payload = b"payload"
    connections = _ScriptedConnections(
        _ServingConnection(
            _Response(200, payload, read_error=OSError("connection reset"))
        ),
        _ServingConnection(_Response(200, payload)),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        connections,
    )

    result = download_asset(
        _hashed_asset(payload),
        tmp_path / "asset",
        _policy(),
        settings=DownloadSettings(
            retry=DownloadRetryPolicy(attempts=2, backoff_seconds=0.5),
            sleep=sleeps.append,
        ),
    )

    assert result == tmp_path / "asset"
    assert connections.opened == 2, "a failed body read must be retried"
    assert sleeps == [0.5], "the retry waits the configured backoff"
    assert result.read_bytes() == payload, "the retry must store the payload"


class _FailingWriter:
    """A destination handle whose body write fails like a full disk."""

    def __init__(self, handle: typ.BinaryIO) -> None:
        """Wrap the real handle so it still closes."""
        self._handle = handle

    def __enter__(self) -> _FailingWriter:
        """Return this writer as the context manager."""
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """Close the real handle and propagate any failure."""
        del exc_info
        self._handle.close()
        return False

    def write(self, data: bytes) -> int:
        """Fail the write as a full disk would."""
        del data
        raise OSError(errno.ENOSPC, "No space left on device")


def test_download_asset_does_not_retry_a_body_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A destination write failure is reported once as the base error class."""
    payload = b"payload"
    connections = _ScriptedConnections(
        _ServingConnection(_Response(200, payload)),
        _ServingConnection(_Response(200, payload)),
    )
    sleeps: list[float] = []
    # `Path.open` is overloaded per mode, so the wrapper forwards whatever the
    # caller passed; the cast keeps that forwarding type-checkable.
    real_open = typ.cast("cabc.Callable[..., typ.BinaryIO]", pathlib.Path.open)

    def failing_open(
        path: pathlib.Path,
        *args: object,
        **kwargs: object,
    ) -> typ.BinaryIO:
        """Wrap the pending download's handle so its body write fails."""
        handle = real_open(path, *args, **kwargs)
        if path.name != "asset.download":
            return handle
        return typ.cast("typ.BinaryIO", _FailingWriter(handle))

    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        connections,
    )
    monkeypatch.setattr(pathlib.Path, "open", failing_open)

    with pytest.raises(ProverToolError) as raised:
        download_asset(
            _hashed_asset(payload),
            tmp_path / "asset",
            _policy(),
            settings=DownloadSettings(
                retry=DownloadRetryPolicy(attempts=2, backoff_seconds=0.5),
                sleep=sleeps.append,
            ),
        )

    assert not isinstance(raised.value, TransientDownloadError), (
        "a destination write failure must not be classified as transient"
    )
    assert connections.opened == 1, "a destination write failure is never retried"
    assert not sleeps, "no backoff occurs for a destination write failure"


@pytest.mark.parametrize("status", [408, 429])
def test_download_asset_retries_a_transient_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A 408 or 429 response is retried and the next attempt is used."""
    payload = b"payload"
    asset = PinnedAsset(
        name=_ASSET,
        url=_RELEASE_URL,
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=1024,
    )
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_open(
        url: str,
        limits: download_transport.DownloadLimits,
        deadline: float,
        *,
        asset_name: str,
    ) -> tuple[_Response, _Connection]:
        """Return the transient status first, then the payload."""
        del url, limits, deadline, asset_name
        calls.append(1)
        if len(calls) == 1:
            return _Response(status), _Connection()
        return _Response(200, payload), _Connection()

    monkeypatch.setattr(download_transport, "_open", fake_open)

    result = download_asset(
        asset,
        tmp_path / "asset",
        _policy(),
        settings=DownloadSettings(
            retry=DownloadRetryPolicy(attempts=2, backoff_seconds=0.0),
            sleep=sleeps.append,
        ),
    )

    assert result == tmp_path / "asset"
    assert len(calls) == 2, "a transient status must be retried"
    assert sleeps == [0.0], "the retry waits the configured backoff"


def test_download_asset_retries_transient_failures_with_bounded_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient transport failures are retried with exponential delays."""
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky(
        asset: PinnedAsset,
        destination: Path,
        limits: DownloadLimits,
        *,
        url_policy: ReleaseUrlPolicy | None = None,
    ) -> Path:
        """Fail twice before succeeding."""
        del limits, url_policy
        calls.append(1)
        if len(calls) < 3:
            msg = "connection reset"
            raise TransientDownloadError(msg)
        destination.write_bytes(b"payload")
        return destination

    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.download.download_pinned", flaky
    )

    result = download_asset(
        _asset(),
        tmp_path / "asset",
        _policy(),
        settings=DownloadSettings(
            retry=DownloadRetryPolicy(
                attempts=3, backoff_seconds=0.5, max_backoff_seconds=0.75
            ),
            sleep=sleeps.append,
        ),
    )

    assert result == tmp_path / "asset"
    assert len(calls) == 3, "the third attempt should succeed"
    assert sleeps == [0.5, 0.75], "backoff must double but stay capped"


def test_download_asset_never_retries_an_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest or policy failure is reported after exactly one attempt."""
    calls: list[int] = []
    sleeps: list[float] = []

    def failing(
        asset: PinnedAsset,
        destination: Path,
        limits: DownloadLimits,
        *,
        url_policy: ReleaseUrlPolicy | None = None,
    ) -> Path:
        """Raise a non-transient integrity failure."""
        del asset, destination, limits, url_policy
        calls.append(1)
        msg = "SHA-256 mismatch"
        raise ProverToolError(msg)

    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.download.download_pinned", failing
    )

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_asset(
            _asset(),
            tmp_path / "asset",
            _policy(),
            settings=DownloadSettings(
                retry=DownloadRetryPolicy(),
                sleep=sleeps.append,
            ),
        )

    assert len(calls) == 1, "integrity errors are never retried"
    assert not sleeps, "no backoff occurs for a permanent failure"


def test_download_asset_does_not_retry_an_unwritable_pending_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local storage failure is reported once as the base error class."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_open(
        url: str,
        limits: download_transport.DownloadLimits,
        deadline: float,
        *,
        asset_name: str,
    ) -> tuple[_Response, _Connection]:
        """Record the attempt and return a successful response."""
        del url, limits, deadline, asset_name
        calls.append(1)
        return _Response(200, b"payload"), _Connection()

    monkeypatch.setattr(download_transport, "_open", fake_open)
    # A directory at the pending path makes the local open fail with EISDIR,
    # which is deterministic and needs no privileged filesystem setup.
    (tmp_path / "asset.download").mkdir()

    with pytest.raises(ProverToolError) as raised:
        download_asset(
            _asset(),
            tmp_path / "asset",
            _policy(),
            settings=DownloadSettings(
                retry=DownloadRetryPolicy(attempts=3, backoff_seconds=0.0),
                sleep=sleeps.append,
            ),
        )

    assert not isinstance(raised.value, TransientDownloadError), (
        "a local storage failure must not be classified as transient"
    )
    assert len(calls) == 1, "a local storage failure is never retried"
    assert not sleeps, "no backoff occurs for a local storage failure"


def test_download_asset_reports_the_last_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted retries surface the transport failure unchanged."""
    calls: list[int] = []

    def failing(
        asset: PinnedAsset,
        destination: Path,
        limits: DownloadLimits,
        *,
        url_policy: ReleaseUrlPolicy | None = None,
    ) -> Path:
        """Fail every attempt."""
        del asset, destination, limits, url_policy
        calls.append(1)
        msg = "connection reset"
        raise TransientDownloadError(msg)

    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.download.download_pinned", failing
    )

    with pytest.raises(TransientDownloadError, match="connection reset"):
        download_asset(
            _asset(),
            tmp_path / "asset",
            _policy(),
            settings=DownloadSettings(
                retry=DownloadRetryPolicy(attempts=2, backoff_seconds=0.0),
                sleep=lambda _: None,
            ),
        )

    assert len(calls) == 2, "attempts must not exceed the configured ceiling"


def test_retry_policy_rejects_an_empty_attempt_count() -> None:
    """A retry policy must permit at least one attempt."""
    with pytest.raises(ProverToolError, match="at least one attempt"):
        DownloadRetryPolicy(attempts=0)
