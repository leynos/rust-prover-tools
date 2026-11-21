"""Bound and render the text that evidence sinks emit.

A proof log can contain a line of arbitrary length, and a JSONL record can
exceed its byte budget through its envelope and escaping alone. These helpers
keep every emitted line within a configured budget, make a JSONL record remain
valid JSON when it must be shortened, and render timestamps and measurements
in forms a human and a machine can both read.

The truncation marker is explicit. A reader must be able to tell a short line
from a cut one, and a cut JSONL record must still parse.
"""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

__all__ = (
    "JSONL_SCHEMA_VERSION",
    "MIN_LINE_BYTES",
    "TRUNCATION_MARKER",
    "bounded_text",
    "fit_record",
    "normalise",
    "utc_text",
)


JSONL_SCHEMA_VERSION: typ.Final[int] = 1

#: Marker appended to a record that exceeded its byte budget.
TRUNCATION_MARKER: typ.Final[str] = "…[truncated]"

#: Smallest accepted record budget. The minimal truncation record must fit.
MIN_LINE_BYTES: typ.Final[int] = 64


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


def _bound_strings(value: object, budget: int) -> object:
    """Return a copy of `value` with every string bounded to `budget` bytes."""
    if isinstance(value, str):
        return bounded_text(value, budget)
    if isinstance(value, dict):
        return {str(key): _bound_strings(item, budget) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_strings(item, budget) for item in value]
    return value


def fit_record(payload: dict[str, object], limit: int) -> str:
    """Serialize one record as valid JSON within `limit` bytes.

    A serialized record can exceed the budget even when every text field is
    inside it, because the JSON envelope and escaping add bytes. Cutting the
    serialized line would leave invalid JSON, so the large string values are
    reduced instead. The final fallback is a minimal, valid truncation record.
    """
    line = json.dumps(payload, ensure_ascii=False)
    if len(line.encode("utf-8")) <= limit:
        return line
    for budget in (4096, 2048, 1024, 512, 256, 128, 64, 32, 16, 8, 0):
        candidate = typ.cast("dict[str, object]", _bound_strings(payload, budget))
        line = json.dumps(candidate, ensure_ascii=False)
        if len(line.encode("utf-8")) <= limit:
            return line
    minimal = {
        "schema": JSONL_SCHEMA_VERSION,
        "record": "truncated",
        "marker": TRUNCATION_MARKER,
    }
    attempts = (
        {
            "original_kind": str(payload.get("record", "")),
            "run_id": str(payload.get("run_id", "")),
        },
        {"original_kind": str(payload.get("record", ""))},
        {},
    )
    for context in attempts:
        fallback = {
            **minimal,
            **{
                key: bounded_text(value, MIN_LINE_BYTES // 2)
                for key, value in context.items()
            },
        }
        line = json.dumps(fallback, separators=(",", ":"), ensure_ascii=False)
        if len(line.encode("utf-8")) <= limit:
            return line
    msg = f"max_line_bytes={limit} is too small for a valid record"
    raise ValueError(msg)


def normalise(value: object) -> object:
    """Render a record value as JSON-compatible data."""
    if isinstance(value, dict):
        return {str(key): normalise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [normalise(item) for item in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    text = getattr(value, "value", None)
    if isinstance(text, str):
        # An enum member is evidence only through its stable name.
        return text
    return str(value)
