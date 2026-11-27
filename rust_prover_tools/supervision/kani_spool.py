"""Spool one verifier log to a temporary file, apart from its live facts.

The observation seam keeps live-fact accumulation free of I/O. This module is
the I/O half: a small protocol an observer writes to and the shared parser
reads once, and a temporary-file implementation that owns its directory.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import tempfile
import typing as typ
from pathlib import Path

__all__ = (
    "KaniSpool",
    "SpoolFactory",
    "TemporaryKaniSpool",
)


class KaniSpool(typ.Protocol):
    """The spool one attempt writes and the shared parser reads once."""

    def write(self, text: str) -> None:
        """Append one piece of the log."""
        ...

    def lines(self) -> cabc.Iterator[str]:
        """Yield the spooled log's physical lines, without terminators."""
        ...

    def close(self) -> None:
        """Release the spool's file and any directory it owns."""
        ...


#: Opens the spool an observer keeps until the attempt boundary closes it.
SpoolFactory = cabc.Callable[[], KaniSpool]


class TemporaryKaniSpool:
    """Spool one log to a file inside a temporary directory it owns."""

    def __init__(self) -> None:
        """Create the temporary directory and the log file it holds."""
        self._stack: contextlib.ExitStack | None = contextlib.ExitStack()
        self._path: Path
        self._handle: typ.IO[str]
        try:
            # pylint: disable-next=consider-using-with
            directory = self._stack.enter_context(tempfile.TemporaryDirectory())
            self._path = Path(directory) / "kani.log"
            # The stack owns the handle; `close` releases both of them.
            # pylint: disable-next=consider-using-with
            handle = self._path.open("w+", encoding="utf-8", errors="replace")
            self._handle = self._stack.enter_context(handle)
        except BaseException:
            self._stack.close()
            self._stack = None
            raise

    def write(self, text: str) -> None:
        """Append one piece of the log to the spool."""
        self._handle.write(text)

    def lines(self) -> cabc.Iterator[str]:
        """Yield the spooled lines, without their terminators."""
        self._handle.flush()
        with self._path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line.removesuffix("\n")

    def close(self) -> None:
        """Release the file and remove the temporary directory, once."""
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
