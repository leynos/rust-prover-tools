"""Write live evidence to a human job log and to versioned JSONL.

Both sinks flush after every record. That is the whole point: the Cuprum source
plan notes that the job log was the only evidence that survived the runner
loss, so an unflushed record is worth nothing when the runner dies. The JSONL
sink also tracks its byte offset, so each harness result can name where its own
evidence begins.

Records are bounded. A verbose prover can emit a line of arbitrary length, and
a single pathological line must not size the run's memory or the log. The
sinks truncate with an explicit marker rather than silently, because a reader
must be able to tell a short line from a cut one.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import os
import typing as typ

from rust_prover_tools.supervision.formatting import (
    JSONL_SCHEMA_VERSION,
    MIN_LINE_BYTES,
    bounded_text,
    fit_record,
    normalise,
    utc_text,
)
from rust_prover_tools.supervision.human import render_human

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.supervision.results import DiagnosticRef

__all__ = (
    "JSONL_SCHEMA_VERSION",
    "MIN_LINE_BYTES",
    "Diagnostics",
    "HumanSink",
    "JsonlSink",
    "NullSink",
    "Sink",
    "bounded_text",
    "fit_record",
    "jsonl_append_offset",
    "render_human",
    "utc_text",
)


#: Version of the JSONL record schema.
class Sink(typ.Protocol):
    """A destination for one evidence record."""

    def write(self, record: dict[str, object]) -> None:
        """Write one record, flushed before returning."""
        ...

    def flush(self) -> None:
        """Flush everything written so far."""
        ...

    @property
    def offset(self) -> int:
        """The number of bytes written so far."""
        ...


def jsonl_append_offset(path: Path) -> int:
    """Return the append offset, adding a line boundary when one is missing.

    A destination that does not end with a newline would make the next record
    share its last line. The inserted byte is included in the returned offset
    so diagnostic references still name the next record exactly.

    Parameters
    ----------
    path : Path
        Existing JSONL destination, or a path that does not yet exist.

    Returns
    -------
    int
        Byte offset at which the next record begins.

    Raises
    ------
    OSError
        The destination exists but cannot be read or repaired.
    """
    if not path.exists():
        return 0
    size = path.stat().st_size
    if size == 0:
        return 0
    with path.open("rb") as reader:
        reader.seek(-1, os.SEEK_END)
        if reader.read(1) == b"\n":
            return size
    with path.open("ab") as writer:
        writer.write(b"\n")
    return size + 1


class JsonlSink:
    """Append one JSON object per line, flushed before returning.

    Attributes
    ----------
    path : Path | None
        Destination path, when the sink owns a file.
    """

    def __init__(
        self,
        handle: typ.TextIO,
        *,
        path: Path | None = None,
        max_line_bytes: int = 8 * 1024,
        start_offset: int = 0,
    ) -> None:
        """Create a sink writing newline-delimited JSON to `handle`.

        Parameters
        ----------
        handle : io.TextIOBase
            Open text destination. The sink never closes it.
        path : Path | None
            Destination path, recorded for diagnostics.
        max_line_bytes : int
            Bound for one record, in bytes.
        start_offset : int
            Bytes already present in an appended destination, so a diagnostic
            reference names the new record's absolute offset.
        """
        if max_line_bytes < MIN_LINE_BYTES:
            msg = "max_line_bytes must be at least 64"
            raise ValueError(msg)
        encoding = getattr(handle, "encoding", None)
        normalised = None if encoding is None else encoding.lower().replace("_", "-")
        if normalised not in {None, "utf-8", "utf8"}:
            msg = f"JsonlSink requires a UTF-8 stream, not {encoding!r}"
            raise ValueError(msg)
        self._handle = handle
        self.path = path
        self._limit = max_line_bytes
        self._offset = start_offset
        self._failed = False

    def write(self, record: dict[str, object]) -> None:
        """Write one record and flush it.

        Parameters
        ----------
        record : dict[str, object]
            Record fields. The schema version is added when absent.

        Notes
        -----
        The stream must be UTF-8 and must not translate newlines. Files opened
        by this package use ``encoding="utf-8", newline=""``; a caller that
        supplies another stream is responsible for the same contract.
        """
        payload = {
            "schema": JSONL_SCHEMA_VERSION,
            **typ.cast("dict[str, object]", normalise(record)),
        }
        line = fit_record(payload, self._limit)
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
        except (OSError, ValueError):  # fmt: skip
            self._failed = True
            raise
        # The handle contract is UTF-8 without newline translation, so the
        # emitted bytes are the encoded line plus one newline byte.
        self._offset += len((line + "\n").encode("utf-8"))

    def flush(self) -> None:
        """Flush everything written so far."""
        try:
            self._handle.flush()
        except (OSError, ValueError):  # fmt: skip
            self._failed = True
            raise

    @property
    def failed(self) -> bool:
        """Whether a write or flush failed and left the offset uncertain."""
        return self._failed

    @property
    def offset(self) -> int:
        """The number of bytes written so far."""
        return self._offset


class HumanSink:
    """Write human-readable lines to a text stream, flushed per line."""

    def __init__(
        self,
        handle: typ.TextIO,
        *,
        max_line_bytes: int = 8 * 1024,
    ) -> None:
        """Create the sink over an open text destination.

        Parameters
        ----------
        handle : io.TextIOBase
            Open text destination. The sink never closes it.
        max_line_bytes : int
            Bound for one line, in bytes.
        """
        if max_line_bytes < MIN_LINE_BYTES:
            msg = f"max_line_bytes must be at least {MIN_LINE_BYTES}"
            raise ValueError(msg)
        self._handle = handle
        self._limit = max_line_bytes
        self._offset = 0

    def write(self, record: dict[str, object]) -> None:
        """Render and write one record, flushing it."""
        line = bounded_text(render_human(record), self._limit)
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
        except BrokenPipeError:
            # The reader went away. Evidence that cannot be delivered must not
            # replace the prover's status; the JSONL sink is the durable copy.
            return
        self._offset += len(line.encode("utf-8")) + 1

    def flush(self) -> None:
        """Flush everything written so far, tolerating a closed reader."""
        with contextlib.suppress(BrokenPipeError):
            self._handle.flush()

    @property
    def offset(self) -> int:
        """The number of bytes written so far."""
        return self._offset


class NullSink:
    """A sink that discards everything, for a disabled destination."""

    @property
    def offset(self) -> int:
        """Always zero, because nothing is written."""
        return 0

    def write(self, record: dict[str, object]) -> None:
        """Discard one record."""

    def flush(self) -> None:
        """Do nothing."""


@dc.dataclass(frozen=True, slots=True)
class Diagnostics:
    """Fan one evidence record out to the configured sinks.

    Attributes
    ----------
    human : Sink
        Human job-log destination.
    jsonl : Sink
        Versioned JSONL destination, whose offset names each harness's
        evidence.
    """

    human: Sink = dc.field(default_factory=NullSink)
    jsonl: Sink = dc.field(default_factory=NullSink)
    _errors: list[Exception] = dc.field(default_factory=list)

    @property
    def error(self) -> Exception | None:
        """The first destination failure, when one occurred."""
        return self._errors[0] if self._errors else None

    def _remember(self, error: Exception) -> None:
        """Retain the first destination failure without interrupting the other."""
        if not self._errors:
            self._errors.append(error)

    def emit(self, record: dict[str, object]) -> DiagnosticRef | None:
        """Write `record` to every sink and return its JSONL reference."""
        from rust_prover_tools.supervision.results import DiagnosticRef

        start = self.jsonl.offset
        jsonl_ok = not (isinstance(self.jsonl, JsonlSink) and self.jsonl.failed)
        if jsonl_ok:
            try:
                self.jsonl.write(record)
            except (OSError, ValueError) as error:
                self._remember(error)
                jsonl_ok = False
        try:
            self.human.write(record)
        except (OSError, ValueError) as error:
            self._remember(error)
        if not jsonl_ok or isinstance(self.jsonl, NullSink):
            return None
        return DiagnosticRef(sink="jsonl", offset=start)

    def flush(self) -> None:
        """Flush both sinks, retaining the first destination failure."""
        sinks = (self.jsonl, self.human)
        if isinstance(self.jsonl, JsonlSink) and self.jsonl.failed:
            sinks = (self.human,)
        for sink in sinks:
            try:
                sink.flush()
            except (OSError, ValueError) as error:
                self._remember(error)
