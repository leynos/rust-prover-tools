"""Tests for the snapshot release URL policy.

The policy pins the exact release URL, the asset name and tag, and the hosts a
release redirect may use. These cases exercise the policy directly and through
the transport's redirect loop, without any retry or download settings.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools import download_transport
from rust_prover_tools.download_transport import DownloadLimits
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.unittests.kani_download_support import (
    ASSET_NAME,
    RELEASE_URL,
    REPOSITORY,
    TAG,
    policy,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_asset_url_is_the_exact_release_path() -> None:
    """The policy builds the only URL a snapshot asset may use."""
    assert policy().asset_url() == RELEASE_URL


@pytest.mark.parametrize(
    "url",
    [
        RELEASE_URL.replace("https://", "http://"),
        RELEASE_URL.replace("github.com", "user:secret@github.com"),
        f"{RELEASE_URL}?download=1",
        RELEASE_URL.replace(REPOSITORY, "someone/else"),
        RELEASE_URL.replace(TAG, "kani-snapshot-2026-09-bbbbbbbbbbbb-r1"),
        RELEASE_URL.replace(ASSET_NAME, "launcher.tar.gz"),
        f"{RELEASE_URL}/extra",
        RELEASE_URL.replace("github.com", "github.com.evil.test"),
        RELEASE_URL.replace("github.com", "127.0.0.1"),
        RELEASE_URL.replace("https://github.com", "https://github.com:8443"),
        f"{RELEASE_URL}#fragment",
        RELEASE_URL.replace(ASSET_NAME, "..%2Fbackend.tar.gz"),
    ],
)
def test_initial_url_outside_the_release_policy_is_rejected(url: str) -> None:
    """Only the pinned repository, tag and asset over HTTPS are accepted."""
    with pytest.raises(ProverToolError, match="release"):
        policy().check_initial(url)


@pytest.mark.parametrize(
    "url",
    [
        (
            f"https://release-assets.githubusercontent.com/github-production/"
            f"release-asset/{REPOSITORY}/123/{ASSET_NAME}?sp=r&sig=abc"
        ),
        (
            f"https://objects.githubusercontent.com/"
            f"github-production-release-asset/123/{ASSET_NAME}?X-Amz-Signature=abc"
        ),
    ],
)
def test_documented_github_asset_redirect_hosts_are_accepted(url: str) -> None:
    """The two GitHub asset redirect hosts may carry a signed query."""
    policy().check_redirect(url)


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
        policy().check_redirect(url)


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
    ) -> tuple[FakeResponse, FakeConnection]:
        """Record the opened URL and return the redirect."""
        del limits, deadline
        opened.append(url)
        return FakeResponse(), connection

    monkeypatch.setattr(download_transport, "_open", fake_open)
    request = download_transport._DownloadRequest(
        RELEASE_URL,
        tmp_path / "asset.download",
        max_bytes=1024,
        asset_name=ASSET_NAME,
        url_policy=policy(),
    )

    with pytest.raises(ProverToolError, match="redirect"):
        download_transport._download(request, DownloadLimits(max_redirects=5))

    assert opened == [RELEASE_URL], "the rejected redirect must not be opened"
    assert connection.closed, "the redirecting connection must close"
