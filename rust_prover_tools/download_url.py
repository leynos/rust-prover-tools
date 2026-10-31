"""HTTPS URL validation for bounded prover downloads.

The downloader accepts only `https` URLs, requires a host, and rejects embedded
credentials before any connection is attempted. Redirect targets pass through
the same policy, so a redirect cannot relax it.

This module is separate from `rust_prover_tools.download_transport` so URL
policy and connection handling can be read and tested independently.
"""

from __future__ import annotations

import dataclasses as dc
import urllib.parse

from rust_prover_tools.errors import ProverToolError


@dc.dataclass(frozen=True, slots=True)
class _HTTPSUrl:
    """Validated HTTPS URL components."""

    host: str
    port: int | None
    path: str
    query: str


def _parse_https_url(url: str) -> _HTTPSUrl:
    """Return parsed URL components after enforcing the HTTPS policy."""
    parsed, port = _split_url(url)
    _require_https_scheme(parsed, url)
    host = _require_host(parsed, url)
    _require_no_credentials(parsed, url)
    return _HTTPSUrl(host=host, port=port, path=parsed.path, query=parsed.query)


def _split_url(url: str) -> tuple[urllib.parse.SplitResult, int | None]:
    """Parse a URL and its port, rejecting values urllib cannot interpret."""
    try:
        parsed = urllib.parse.urlsplit(url)
        # Accessing ``port`` may itself raise for a malformed port.
        port = parsed.port
    except ValueError as exc:
        msg = f"invalid download URL '{url}': {exc}"
        raise ProverToolError(msg) from exc
    return parsed, port


def _require_https_scheme(parsed: urllib.parse.SplitResult, url: str) -> None:
    """Reject a URL whose scheme is not HTTPS."""
    if parsed.scheme.lower() != "https":
        msg = f"download URL must use https, got '{url}'"
        raise ProverToolError(msg)


def _require_host(parsed: urllib.parse.SplitResult, url: str) -> str:
    """Return the URL host, rejecting an address without one."""
    host = parsed.hostname
    if host is None:
        msg = f"download URL has no host: '{url}'"
        raise ProverToolError(msg)
    return host


def _require_no_credentials(parsed: urllib.parse.SplitResult, url: str) -> None:
    """Reject embedded credentials, which would otherwise reach the network."""
    if parsed.username is not None or parsed.password is not None:
        msg = f"download URL must not contain credentials: '{url}'"
        raise ProverToolError(msg)


def _validate_url(url: str) -> str:
    """Validate a URL and return it unchanged for redirect bookkeeping."""
    _parse_https_url(url)
    return url
