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
        self._handle = handle
        self.path = path
        self._limit = max_line_bytes
        self._offset = start_offset

    def write(self, record: dict[str, object]) -> None:
        """Write one record and flush it.

        Parameters
        ----------
        record : dict[str, object]
            Record fields. The schema version is added when absent.
        """
        payload = {
            "schema": JSONL_SCHEMA_VERSION,
            **typ.cast("dict[str, object]", normalise(record)),
        }
        line = fit_record(payload, self._limit)
        self._handle.write(line + "\n")
        self._handle.flush()
        self._offset += len(line.encode("utf-8")) + 1

    def flush(self) -> None:
        """Flush everything written so far."""
        self._handle.flush()

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

    def emit(self, record: dict[str, object]) -> DiagnosticRef | None:
        """Write `record` to every sink and return its JSONL reference."""
        from rust_prover_tools.supervision.results import DiagnosticRef

        start = self.jsonl.offset
        self.jsonl.write(record)
        self.human.write(record)
        if isinstance(self.jsonl, NullSink):
            return None
        return DiagnosticRef(sink="jsonl", offset=start)

    def flush(self) -> None:
        """Flush both sinks."""
        self.jsonl.flush()
        self.human.flush()
