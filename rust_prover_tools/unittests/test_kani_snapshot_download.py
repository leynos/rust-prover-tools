"""Tests for snapshot release URL policy and bounded retrying downloads."""

from __future__ import annotations

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
