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

import dataclasses as dc
import datetime as dt
import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import io
    from pathlib import Path

    from rust_prover_tools.supervision.results import DiagnosticRef

__all__ = (
    "JSONL_SCHEMA_VERSION",
    "Diagnostics",
    "HumanSink",
    "JsonlSink",
    "NullSink",
    "Sink",
    "bounded_text",
    "render_human",
    "utc_text",
)

#: Version of the JSONL record schema.
JSONL_SCHEMA_VERSION: typ.Final[int] = 1

#: Marker appended to a record that exceeded its byte budget.
TRUNCATION_MARKER: typ.Final[str] = "…[truncated]"


def utc_text(moment: dt.datetime) -> str:
    """Render a timestamp in a stable, timezone-explicit form.

    Parameters
    ----------
    moment : datetime.datetime
        Timezone-aware time.

    Returns
    -------
    str
        RFC 3339 text with a ``Z`` suffix and millisecond precision.
    """
    return (
        moment
        .astimezone(tz=dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def bounded_text(text: str, limit: int) -> str:
    """Return `text` truncated to at most `limit` bytes with an explicit marker.

    Parameters
    ----------
    text : str
        Text to bound.
    limit : int
        Maximum encoded bytes, including the marker.

    Returns
    -------
    str
        The original text, or a UTF-8-safe prefix followed by the marker.
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    room = max(0, limit - len(TRUNCATION_MARKER.encode("utf-8")))
    return raw[:room].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _normalise(value: object) -> object:
    """Render a record value as JSON-compatible data."""
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    text = getattr(value, "value", None)
    if isinstance(text, str):
        # An enum member is evidence only through its stable name.
        return text
    return str(value)


def _measurement(value: object) -> str:
    """Render a measurement for the human log."""
    document = typ.cast("dict[str, object]", value)
    if document.get("value") is not None:
        return f"{document['value']}"
    return f"unavailable({document.get('unavailable_reason', 'unknown')})"


def render_human(record: dict[str, object]) -> str:
    """Render one evidence record as a human-readable line.

    Parameters
    ----------
    record : dict[str, object]
        Record fields written to the JSONL sink.

    Returns
    -------
    str
        One line without a terminator.
    """
    formatter = _FORMATTERS.get(typ.cast("str", record.get("record")))
    if formatter is None:
        payload = json.dumps(_normalise(record), ensure_ascii=False)
        return f"[{_stamp(record)}] {payload}"
    return formatter(record)


def _render_run_start(record: dict[str, object]) -> str:
    """Render the run's opening record."""
    return (
        f"[{_stamp(record)}] run {record['run_id']} start: "
        f"{record['harness_count']} harness(es), "
        f"timeout={record['timeout_seconds']}s, "
        f"sample={record['sample_interval_seconds']}s, "
        f"watchdog={record['watchdog_mib']}MiB"
    )


def _render_harness_start(record: dict[str, object]) -> str:
    """Render one invocation's opening record."""
    return f"[{_stamp(record)}] {_subject(record)} attempt {record['attempt']} start"


def _render_output(record: dict[str, object]) -> str:
    """Render one streamed output line."""
    return f"[{_stamp(record)}] {_subject(record)} {record['stream']}: {record['text']}"


def _render_heartbeat(record: dict[str, object]) -> str:
    """Render one periodic heartbeat."""
    return (
        f"[{_stamp(record)}] {_subject(record)} heartbeat: "
        f"elapsed={record['elapsed_seconds']}s idle={record['idle_seconds']}s "
        f"rss={_measurement(record['group_rss'])} "
        f"host-available={_measurement(record['host_available'])}"
    )


def _render_sample(record: dict[str, object]) -> str:
    """Render one resource sample."""
    return (
        f"[{_stamp(record)}] {_subject(record)} sample: "
        f"elapsed={record['elapsed_seconds']}s "
        f"rss={_measurement(record['group_rss'])} "
        f"host-available={_measurement(record['host_available'])} "
        f"host-total={_measurement(record['host_total'])}"
    )


def _render_harness_finish(record: dict[str, object]) -> str:
    """Render one harness's closing record."""
    return (
        f"[{_stamp(record)}] {_subject(record)} finish: status={record['status']} "
        f"stop={record['stop_reason']} exit={record['exit_code']} "
        f"verdict={record['proof_verdict']} "
        f"duration={record['duration_seconds']}s "
        f"peak-rss={_measurement(record['peak_group_rss'])} "
        f"min-available={_measurement(record['min_host_available'])}"
    )


def _render_run_finish(record: dict[str, object]) -> str:
    """Render the run's closing record."""
    return (
        f"[{_stamp(record)}] run {record['run_id']} finish: "
        f"outcome={record['outcome']} complete={record['complete']} "
        f"interrupted={record['interrupted']}"
    )


def _render_skipped(record: dict[str, object]) -> str:
    """Render one harness that never ran."""
    return f"[{_stamp(record)}] {_subject(record)} not run: {record['detail']}"


#: One renderer per record kind. A new record kind needs an entry here or it
#: falls back to a JSON line, which is readable but less scannable.
_FORMATTERS: typ.Final[dict[str, cabc.Callable[[dict[str, object]], str]]] = {
    "run-start": _render_run_start,
    "harness-start": _render_harness_start,
    "output": _render_output,
    "heartbeat": _render_heartbeat,
    "sample": _render_sample,
    "harness-finish": _render_harness_finish,
    "harness-skipped": _render_skipped,
    "run-finish": _render_run_finish,
}


def _stamp(record: dict[str, object]) -> str:
    """Render a record's timestamp for the human log."""
    moment = record.get("timestamp")
    if isinstance(moment, dt.datetime):
        return utc_text(moment)
    return str(moment if moment is not None else "")


def _subject(record: dict[str, object]) -> str:
    """Return the package-qualified harness subject of a record."""
    return f"{record.get('package')}::{record.get('harness')}"


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
        handle: io.TextIOBase,
        *,
        path: Path | None = None,
        max_line_bytes: int = 8 * 1024,
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
        """
        self._handle = handle
        self.path = path
        self._limit = max_line_bytes
        self._offset = 0

    def write(self, record: dict[str, object]) -> None:
        """Write one record and flush it.

        Parameters
        ----------
        record : dict[str, object]
            Record fields. The schema version is added when absent.
        """
        payload = {
            "schema": JSONL_SCHEMA_VERSION,
            **typ.cast("dict[str, object]", _normalise(record)),
        }
        line = bounded_text(json.dumps(payload, ensure_ascii=False), self._limit)
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
        handle: io.TextIOBase,
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
        """Flush everything written so far."""
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
