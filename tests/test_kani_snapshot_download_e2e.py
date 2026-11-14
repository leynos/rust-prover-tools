"""End-to-end tests for the snapshot download boundary.

These tests speak HTTPS to a real loopback server. Nothing between
`download_asset` and its socket is replaced: the TLS handshake, redirect hop,
retry loop, streamed body and digest check all run as in production. Only the
URL policy is test-local, because the production policy pins `github.com` on
the default HTTPS port.
"""

from __future__ import annotations

import hashlib
import typing as typ
import urllib.parse

import pytest

from rust_prover_tools.downloads import PinnedAsset
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.download import (
    DownloadRetryPolicy,
    DownloadSettings,
    download_asset,
)
from tests.kani_snapshot_tls_fixtures import (
    PRIVATE_KEY_PEM,
    Reply,
    loopback_release_policy,
    tls_server,
    trust_loopback_certificate,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from tests.kani_snapshot_tls_fixtures import LoopbackReleasePolicy


@pytest.fixture
def tls_material(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Write the loopback certificate and key and trust the certificate."""
    certificate = tmp_path / "loopback-cert.pem"
    private_key = tmp_path / "loopback-key.pem"
    private_key.write_text(PRIVATE_KEY_PEM, encoding="utf-8")
    trust_loopback_certificate(monkeypatch, tmp_path)
    return certificate, private_key


def _release_path(asset_name: str) -> str:
    """Return the request path of a loopback release URL for `asset_name`."""
    policy = loopback_release_policy("https://loopback.invalid", asset_name)
    return urllib.parse.urlsplit(policy.asset_url()).path


def _constant(reply: Reply) -> cabc.Callable[[int], Reply]:
    """Return a route that answers every request with `reply`."""
    return lambda _hit: reply


def _asset(
    policy: LoopbackReleasePolicy,
    payload: bytes,
    *,
    digest: str | None = None,
) -> PinnedAsset:
    """Return the pinned asset the loopback policy describes."""
    return PinnedAsset(
        name=policy.asset_name,
        url=policy.asset_url(),
        sha256=digest if digest is not None else hashlib.sha256(payload).hexdigest(),
        max_bytes=len(payload) + 1024,
    )


def test_https_response_is_streamed_and_digest_verified(
    tls_material: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """One HTTPS request carries the body that the digest check accepts."""
    certificate, private_key = tls_material
    payload = b"snapshot asset body" * 257
    path = _release_path("backend.tar.gz")
    destination = tmp_path / "backend.tar.gz"

    routes = {path: _constant(Reply(body=payload))}
    with tls_server(routes, certificate, private_key) as server:
        policy = loopback_release_policy(server.origin, "backend.tar.gz")
        returned = download_asset(_asset(policy, payload), destination, policy)

    assert returned == destination
    assert destination.read_bytes() == payload, "the TLS body was not streamed intact"
    assert server.requests == [path], "the release URL was not the first request"


def test_redirect_to_an_allowed_host_is_followed(
    tls_material: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A redirect hop is policy-checked and then streamed from its target."""
    certificate, private_key = tls_material
    payload = b"redirected snapshot asset"
    path = _release_path("redirect.bin")
    target = "/assets/redirect.bin"
    routes = {
        path: _constant(Reply(status=302, headers=(("Location", target),))),
        target: _constant(Reply(body=payload)),
    }

    with tls_server(routes, certificate, private_key) as server:
        policy = loopback_release_policy(server.origin, "redirect.bin")
        download_asset(_asset(policy, payload), tmp_path / "redirect.bin", policy)

    assert (tmp_path / "redirect.bin").read_bytes() == payload
    assert server.requests == [path, target], "the policy did not admit the redirect"


def test_a_redirect_off_the_allowed_hosts_is_refused(
    tls_material: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A redirect leaving the allowed host list is refused before it is opened."""
    certificate, private_key = tls_material
    path = _release_path("away.bin")
    elsewhere = "https://example.test/assets/away.bin"
    routes = {path: _constant(Reply(status=302, headers=(("Location", elsewhere),)))}

    with tls_server(routes, certificate, private_key) as server:
        policy = loopback_release_policy(server.origin, "away.bin")
        with pytest.raises(ProverToolError, match="redirect host is not allowed"):
            download_asset(
                _asset(policy, b"never fetched"),
                tmp_path / "away.bin",
                policy,
            )

    assert server.requests == [path], "the disallowed redirect target was contacted"


def test_a_transient_status_is_retried_over_https(
    tls_material: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A 503 is retried by `download_asset` and the retry succeeds."""
    certificate, private_key = tls_material
    payload = b"retried snapshot asset"
    path = _release_path("retry.bin")
    replies = {1: Reply(status=503), 2: Reply(body=payload)}
    sleeps: list[float] = []
    settings = DownloadSettings(
        retry=DownloadRetryPolicy(
            attempts=2,
            backoff_seconds=0.5,
            max_backoff_seconds=0.5,
        ),
        sleep=sleeps.append,
    )

    routes = {path: lambda hit: replies[hit]}
    with tls_server(routes, certificate, private_key) as server:
        policy = loopback_release_policy(server.origin, "retry.bin")
        download_asset(
            _asset(policy, payload), tmp_path / "retry.bin", policy, settings
        )

    assert sleeps == [0.5], "the retry did not wait the policy's delay"
    assert server.requests == [path, path], "the transient failure was not retried"
    assert (tmp_path / "retry.bin").read_bytes() == payload


def test_a_digest_mismatch_is_rejected_after_the_body_arrives(
    tls_material: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A complete HTTPS body with the wrong digest never reaches the caller."""
    certificate, private_key = tls_material
    path = _release_path("tampered.bin")
    destination = tmp_path / "tampered.bin"

    with tls_server(
        {path: _constant(Reply(body=b"tampered snapshot asset"))},
        certificate,
        private_key,
    ) as server:
        policy = loopback_release_policy(server.origin, "tampered.bin")
        asset = _asset(policy, b"tampered snapshot asset", digest="0" * 64)
        with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
            download_asset(asset, destination, policy)

    assert not destination.exists(), "an unverified body reached the final path"
    assert not destination.with_name(f"{destination.name}.download").exists()
    assert server.requests == [path], "the mismatch was judged without a download"
