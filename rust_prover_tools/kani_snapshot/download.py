"""Narrow URL policy and bounded retries for snapshot asset downloads.

The generic downloader accepts any HTTPS host. Snapshot assets are stricter:
the first request must be the exact GitHub release path for the pinned
repository, tag and asset, and every redirect must stay on a documented GitHub
asset host. Only transient transport failures are retried; integrity, policy
and local filesystem failures are reported after one attempt.

Example
-------
```python
policy = ReleaseUrlPolicy.for_pin(pin, "backend.tar.gz")
asset = PinnedAsset(
    name="backend.tar.gz",
    url=policy.asset_url(),
    sha256="0" * 64,
    max_bytes=2 * 1024 * 1024 * 1024,
)
download_asset(asset, Path("backend.tar.gz"), policy)
```
"""

from __future__ import annotations

import dataclasses as dc
import logging
import time
import typing as typ
import urllib.parse

from rust_prover_tools.downloads import (
    DEFAULT_DOWNLOAD_LIMITS,
    PinnedAsset,
    download_pinned,
)
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.policy import is_safe_asset_name
from rust_prover_tools.kani_snapshot.tags import TAG_PATTERN
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.download_transport import DownloadLimits
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "DEFAULT_DOWNLOAD_SETTINGS",
    "DEFAULT_RETRY_POLICY",
    "REDIRECT_HOSTS",
    "DownloadRetryPolicy",
    "DownloadSettings",
    "ReleaseUrlPolicy",
    "download_asset",
)

_LOGGER = logging.getLogger(__name__)

#: Hosts GitHub uses for release asset bodies after the first redirect.
REDIRECT_HOSTS: typ.Final[frozenset[str]] = frozenset({
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

_DEFAULT_BACKOFF_SECONDS: typ.Final[float] = 2.0
_DEFAULT_MAX_BACKOFF_SECONDS: typ.Final[float] = 10.0
_DEFAULT_ATTEMPTS: typ.Final[int] = 3
_DEFAULT_HTTPS_PORT: typ.Final[int] = 443


@dc.dataclass(frozen=True, slots=True)
class ReleaseUrlPolicy:
    """The exact release URL and redirect hosts one asset may use.

    Attributes
    ----------
    repository : str
        Publishing repository, exactly `leynos/rust-prover-tools`.
    tag : str
        Immutable release tag whose assets are being fetched.
    asset_name : str
        Safe release asset basename.
    redirect_hosts : frozenset[str]
        Documented GitHub asset hosts allowed after the initial request.
    """

    repository: str
    tag: str
    asset_name: str
    redirect_hosts: frozenset[str] = REDIRECT_HOSTS

    def __post_init__(self) -> None:
        """Reject values that cannot form one exact release URL."""
        if self.repository != REPOSITORY:
            msg = f"snapshot repository must be {REPOSITORY}, got '{self.repository}'"
            raise ProverToolError(msg)
        if TAG_PATTERN.fullmatch(self.tag) is None:
            msg = f"snapshot tag is outside the release grammar: '{self.tag}'"
            raise ProverToolError(msg)
        if not is_safe_asset_name(self.asset_name):
            msg = f"snapshot asset name is not a safe basename: '{self.asset_name}'"
            raise ProverToolError(msg)
        for host in self.redirect_hosts:
            _require_plain_host(host)

    @classmethod
    def for_pin(cls, pin: SnapshotPin, asset_name: str) -> ReleaseUrlPolicy:
        """Return the policy a reviewed pin fixes for one asset.

        Parameters
        ----------
        pin : SnapshotPin
            Reviewed pin whose repository and tag define the release.
        asset_name : str
            Safe release asset basename.

        Returns
        -------
        ReleaseUrlPolicy
            Policy for that exact asset.
        """
        return cls(repository=pin.repository, tag=pin.tag, asset_name=asset_name)

    def asset_url(self) -> str:
        """Return the only URL from which the asset may be downloaded."""
        return (
            f"https://github.com/{self.repository}/releases/download/"
            f"{self.tag}/{self.asset_name}"
        )

    def check_initial(self, url: str) -> None:
        """Require the exact release URL for the configured asset.

        Parameters
        ----------
        url : str
            First URL of a download.

        Raises
        ------
        ProverToolError
            Raised for any scheme, host, path, query, credential or port
            outside the exact release URL.
        """
        parsed = _require_common(url, context="release")
        expected = self.asset_url()
        if url != expected:
            msg = f"snapshot release URL must be exactly '{expected}', got '{url}'"
            raise ProverToolError(msg)
        if parsed.query:
            msg = f"snapshot release URL must not contain a query: '{url}'"
            raise ProverToolError(msg)

    def check_redirect(self, url: str) -> None:
        """Require a redirect to stay on a documented GitHub asset host.

        Parameters
        ----------
        url : str
            Redirect target before it is opened.

        Raises
        ------
        ProverToolError
            Raised for an unlisted host, a non-HTTPS scheme, credentials, a
            non-default port, a fragment, or a traversal path.
        """
        parsed = _require_common(url, context="redirect")
        if parsed.hostname not in self.redirect_hosts:
            msg = f"snapshot redirect host is not allowed: '{parsed.hostname}'"
            raise ProverToolError(msg)
        _require_contained_path(parsed.path, url)


def _is_plain_host(host: str) -> bool:
    """Return whether a redirect host is a plain lowercase DNS name."""
    return (
        bool(host)
        and host == host.lower()
        and not any(char in host for char in ":/\\?#@ \t\r\n\x00")
    )


def _require_plain_host(host: str) -> None:
    """Reject a redirect host that is not a plain lowercase DNS name."""
    if not _is_plain_host(host):
        msg = f"snapshot redirect host is not a plain DNS name: '{host}'"
        raise ProverToolError(msg)


def _require_common(url: str, *, context: str) -> urllib.parse.SplitResult:
    """Apply the scheme, credential, port and fragment rules."""
    if not url or any(not char.isprintable() for char in url):
        msg = f"snapshot {context} URL contains a control character"
        raise ProverToolError(msg)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        msg = f"invalid snapshot {context} URL '{url}': {exc}"
        raise ProverToolError(msg) from exc
    if parsed.scheme != "https":
        msg = f"snapshot {context} URL must use https: '{url}'"
        raise ProverToolError(msg)
    if parsed.hostname is None:
        msg = f"snapshot {context} URL has no host: '{url}'"
        raise ProverToolError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = f"snapshot {context} URL must not contain credentials: '{url}'"
        raise ProverToolError(msg)
    if port not in {None, _DEFAULT_HTTPS_PORT}:
        msg = f"snapshot {context} URL must use the default HTTPS port: '{url}'"
        raise ProverToolError(msg)
    if parsed.fragment:
        msg = f"snapshot {context} URL must not contain a fragment: '{url}'"
        raise ProverToolError(msg)
    return parsed


def _is_unsafe_path(path: str) -> bool:
    """Return whether a decoded redirect path is absolute or hostile."""
    return not path.startswith("/") or "\\" in path or "\x00" in path


def _require_contained_path(path: str, url: str) -> None:
    """Reject a redirect path that is absolute-relative or traversing."""
    decoded = urllib.parse.unquote(path)
    if _is_unsafe_path(decoded):
        msg = f"snapshot redirect URL has an unsafe path: '{url}'"
        raise ProverToolError(msg)
    if ".." in decoded.split("/"):
        msg = f"snapshot redirect URL traverses its host path: '{url}'"
        raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class DownloadRetryPolicy:
    """Bounded retry settings for one snapshot asset.

    Attributes
    ----------
    attempts : int
        Maximum attempts, including the first. Defaults to 3.
    backoff_seconds : float
        Delay before the second attempt. Later delays double. Defaults to 2.
    max_backoff_seconds : float
        Ceiling for one delay. Defaults to 10 seconds.
    """

    attempts: int = _DEFAULT_ATTEMPTS
    backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS
    max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        """Reject settings that would never run or would wait forever."""
        if self.attempts < 1:
            msg = "download retry policy needs at least one attempt"
            raise ProverToolError(msg)
        for name, value in (
            ("backoff_seconds", self.backoff_seconds),
            ("max_backoff_seconds", self.max_backoff_seconds),
        ):
            if value < 0:
                msg = f"download retry policy {name} must not be negative"
                raise ProverToolError(msg)

    def delay(self, failed_attempt: int) -> float:
        """Return the bounded delay after a failed attempt number."""
        delay = self.backoff_seconds * 2 ** (failed_attempt - 1)
        return min(delay, self.max_backoff_seconds)


DEFAULT_RETRY_POLICY: typ.Final[DownloadRetryPolicy] = DownloadRetryPolicy()


@dc.dataclass(frozen=True, slots=True)
class DownloadSettings:
    """Transport limits, retry policy and delay function for one download.

    Attributes
    ----------
    limits : DownloadLimits
        Network limits. Defaults to the downloader's defaults.
    retry : DownloadRetryPolicy
        Bounded retry settings for transient failures.
    sleep : Callable[[float], None]
        Delay function, injectable for deterministic tests.
    """

    limits: DownloadLimits = DEFAULT_DOWNLOAD_LIMITS
    retry: DownloadRetryPolicy = DEFAULT_RETRY_POLICY
    sleep: cabc.Callable[[float], None] = time.sleep


DEFAULT_DOWNLOAD_SETTINGS: typ.Final[DownloadSettings] = DownloadSettings()


def download_asset(
    asset: PinnedAsset,
    destination: Path,
    policy: ReleaseUrlPolicy,
    settings: DownloadSettings = DEFAULT_DOWNLOAD_SETTINGS,
) -> Path:
    """Download one snapshot asset under its release URL policy.

    Parameters
    ----------
    asset : PinnedAsset
        Pinned asset bytes and digest.
    destination : Path
        Final path for the verified asset.
    policy : ReleaseUrlPolicy
        Exact release URL and redirect host policy.
    settings : DownloadSettings
        Transport limits, retry policy and delay function.

    Returns
    -------
    Path
        The verified destination path.

    Raises
    ------
    ProverToolError
        Raised for a mismatched asset or any non-retryable failure.
    TransientDownloadError
        Raised when every attempt failed transiently.
    """
    if asset.name != policy.asset_name:
        msg = f"asset '{asset.name}' does not match policy '{policy.asset_name}'"
        raise ProverToolError(msg)
    if asset.url != policy.asset_url():
        msg = "snapshot asset URL is not the policy's exact release URL"
        raise ProverToolError(msg)
    for attempt in range(1, settings.retry.attempts + 1):
        try:
            return download_pinned(
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
                asset=asset.name,
                attempt=attempt,
                delay_seconds=delay,
            )
            settings.sleep(delay)
    msg = "download retry loop ended without a result"
    raise AssertionError(msg)
