"""Property tests for the bounded download transport.

The unit tests cover specific limit failures. These properties make the
invariants explicit over arbitrary chunk sequences and redirect budgets,
because a hostile or broken response controls both.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rust_prover_tools import download_transport
from rust_prover_tools.download_transport import DownloadLimits
from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

# Byte-limit properties keep the deadline open so only size can stop the read.
NO_DEADLINE = float("inf")

# Hypothesis cannot reset `tmp_path` or `monkeypatch` between examples because
# both are teardown fixtures. Each example creates its own files and installs
# the same replacement, so reuse is safe.
REUSABLE_FIXTURES = [HealthCheck.function_scoped_fixture]


class ChunkResponse:
    """A response that yields predetermined chunks without a length header."""

    status = 200

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self._index = 0

    def getheader(self, name: str, default: str | None = None) -> str | None:
        """Report no headers, including `Content-Length`."""
        del name
        return default

    def read(self, amt: int | None = None) -> bytes:
        """Return the next chunk, or an empty chunk at end of body."""
        del amt
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class RedirectResponse:
    """A response that always redirects to another HTTPS location."""

    status = 302

    def getheader(self, name: str, default: str | None = None) -> str | None:
        """Return a redirect target for `Location` and no other header."""
        if name == "Location":
            return "https://example.test/next"
        return default

    def read(self, amt: int | None = None) -> bytes:
        """Return an empty body; a redirect has none to copy."""
        del amt
        return b""


class ClosingConnection:
    """A connection that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        """Record closure."""
        self.closed = True


@given(
    # An empty chunk is the EOF marker for a streamed response, so generated
    # chunks must be non-empty to describe a body the reader can observe.
    chunks=st.lists(st.binary(min_size=1, max_size=32), max_size=6),
    max_bytes=st.integers(min_value=0, max_value=64),
)
@settings(max_examples=50, deadline=None, suppress_health_check=REUSABLE_FIXTURES)
def test_stream_never_writes_past_the_byte_ceiling(
    tmp_path: Path,
    chunks: list[bytes],
    max_bytes: int,
) -> None:
    """A streamed body is either accepted whole or stopped at the ceiling."""
    destination = tmp_path / "asset.bin"
    response = ChunkResponse(tuple(chunks))

    try:
        download_transport._stream(
            response,
            destination,
            max_bytes=max_bytes,
            deadline=NO_DEADLINE,
        )
    except ProverToolError:
        assert destination.stat().st_size <= max_bytes, (
            "a rejected stream must not keep bytes above the ceiling"
        )
    else:
        body = b"".join(chunks)
        assert len(body) <= max_bytes, "an accepted body must fit the ceiling"
        assert destination.read_bytes() == body, (
            "an accepted body must be written in full"
        )


@given(max_redirects=st.integers(min_value=0, max_value=5))
@settings(max_examples=10, deadline=None, suppress_health_check=REUSABLE_FIXTURES)
def test_download_never_exceeds_the_redirect_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_redirects: int,
) -> None:
    """An endless redirect chain stops after the configured number of hops."""
    opened: list[str] = []

    def always_redirect(
        url: str,
        limits: DownloadLimits,
        deadline: float,
    ) -> tuple[RedirectResponse, ClosingConnection]:
        del limits, deadline
        opened.append(url)
        return RedirectResponse(), ClosingConnection()

    monkeypatch.setattr(download_transport, "_open", always_redirect)

    with pytest.raises(ProverToolError, match="too many redirects"):
        download_transport._download(
            "https://example.test/start",
            tmp_path / "asset.bin",
            max_bytes=16,
            limits=DownloadLimits(max_redirects=max_redirects),
        )

    assert len(opened) == max_redirects + 1, (
        "the download must make exactly one request per allowed hop plus the "
        "rejected hop"
    )
