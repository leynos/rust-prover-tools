"""Render evidence records as scannable human job-log lines.

The human sink is read during and after a run, often from a CI log viewer. Each
record kind has one stable line shape so a reader can scan identities, statuses,
durations, and resource values without parsing JSON.
"""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

from rust_prover_tools.supervision.formatting import normalise, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("render_human",)


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
        payload = json.dumps(normalise(record), ensure_ascii=False)
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
