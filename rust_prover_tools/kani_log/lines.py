"""Line sources the Kani parser can read from.

A parser call needs random access to a small lookahead, not the whole log.
These sources supply that: one wraps a list a caller already holds, and one
streams an iterator while retaining only the window the cursor can still
address.
"""

from __future__ import annotations

import collections
import typing as typ

from rust_prover_tools.kani_log.syntax import KaniLogError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("LineSource", "ListLines", "StreamingLines")


class LineSource(typ.Protocol):
    """A random-access view of log lines that need not all be in memory."""

    def at(self, index: int) -> str | None:
        """Return the line at `index`, or `None` past the end."""
        ...

    def release(self, index: int) -> None:
        """Discard every line before `index`, which the cursor has passed."""
        ...


class ListLines:
    """A line source backed by a list the caller already holds."""

    def __init__(self, lines: cabc.Iterable[str]) -> None:
        """Store the lines for random access."""
        self._lines = list(lines)

    def at(self, index: int) -> str | None:
        """Return the line at `index`, or `None` past the end."""
        if index >= len(self._lines):
            return None
        return self._lines[index]

    @staticmethod
    def release(index: int) -> None:
        """Retain the list; nothing is discarded."""
        del index


class StreamingLines:
    """A bounded window over an iterator of log lines.

    The shared parser needs a small lookahead, never the whole log. This source
    pulls lines on demand, keeps only the window the cursor can still address,
    and drops each line once the parser has consumed it.
    """

    def __init__(self, lines: cabc.Iterable[str]) -> None:
        """Wrap `lines` without reading more than one line ahead of the cursor."""
        self._lines = iter(lines)
        self._buffer: collections.deque[str] = collections.deque()
        self._base = 0
        self._eof = False

    def at(self, index: int) -> str | None:
        """Return the line at `index`, pulling from the iterator as needed."""
        if index < self._base:
            msg = f"line {index + 1} was released before the parser read it"
            raise KaniLogError(msg)
        while self._base + len(self._buffer) <= index and not self._eof:
            try:
                self._buffer.append(next(self._lines))
            except StopIteration:
                self._eof = True
        offset = index - self._base
        if offset >= len(self._buffer):
            return None
        return self._buffer[offset]

    @property
    def buffered(self) -> int:
        """The number of lines the window currently holds."""
        return len(self._buffer)

    def release(self, index: int) -> None:
        """Drop every line before `index`."""
        while self._base < index and self._buffer:
            self._buffer.popleft()
            self._base += 1
