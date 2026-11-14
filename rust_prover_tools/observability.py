"""Opt-in structured events for operational boundaries.

The library installs no logging handlers and emits nothing by default. An
application opts in by lowering the level of the relevant module logger, for
example ``logging.getLogger("rust_prover_tools").setLevel(logging.DEBUG)``.
Each event carries a bounded ``event_data`` mapping on the log record so a
collector can read fields without parsing the message text.

Events are also the metric source: outcome, latency and cache fields carry a
closed set of short category strings or a number, never a path, URL, argument
list, digest or payload. A collector can therefore aggregate them directly
without unbounded label growth. The one exception is ``span_id``: it is unique
per operation, so it is a log-only correlation key and must never become a
metric label.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import itertools
import time
import typing as typ

__all__ = ("OperationSpan", "log_event", "operation_span")

#: Process-local span counter. `next` on a `count` is atomic, so concurrent
#: spans never share an identifier.
_SPAN_IDS = itertools.count(1)

if typ.TYPE_CHECKING:
    # Annotations are strings, so these modules are needed only for checking.
    import collections.abc as cabc
    import logging


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit one structured event at debug level.

    Parameters
    ----------
    logger : logging.Logger
        Module logger that owns the event. Passing it explicitly keeps an
        application's per-module level selection meaningful.
    event : str
        Stable dotted event name, such as ``download.failed``.
    **fields : object
        Bounded values such as the asset name or a limit category. Never pass
        a URL that may carry a query token, or any file content.
    """
    logger.debug(event, extra={"event_data": {"event": event, **fields}})


@dc.dataclass(slots=True)
class OperationSpan:
    """Completion facts an instrumented operation learns while it runs.

    Attributes
    ----------
    outcome : str
        Bounded outcome category reported by the completion event.
    fields : dict[str, object]
        Additional bounded fields merged into the completion event only, for
        facts such as a cache hit that are unknown when the span starts.
    """

    outcome: str = "complete"
    fields: dict[str, object] = dc.field(default_factory=dict)

    def finish(self, outcome: str, **fields: object) -> None:
        """Record the final outcome and any extra completion fields.

        Parameters
        ----------
        outcome : str
            Bounded category describing how the operation ended.
        **fields : object
            Bounded completion-only fields, such as ``cache="hit"``.
        """
        self.outcome = outcome
        self.fields.update(fields)


def _elapsed_seconds(started: float) -> float:
    """Return seconds since `started`, rounded to microseconds."""
    return round(time.monotonic() - started, 6)


@contextlib.contextmanager
def operation_span(
    logger: logging.Logger,
    event: str,
    **fields: object,
) -> cabc.Generator[OperationSpan]:
    """Correlate one operation's start with its completion or failure.

    The closing event repeats the start event's fields and adds
    ``elapsed_seconds``, so a collector can join the pair on the event prefix
    and treat it as a timed span without a tracing dependency. A failure closes
    the span with the exception's class name rather than its message, because a
    message can embed a path.

    Every event also carries a process-local ``span_id``. Concurrent spans of
    the same operation would otherwise be indistinguishable once their events
    interleave, and a collector could pair one operation's start with another's
    completion. The identifier is unique per span, so it is log-only: never
    emit it as a metric label, which would grow without bound.

    Parameters
    ----------
    logger : logging.Logger
        Module logger that owns the events.
    event : str
        Stable dotted name without a suffix, such as
        ``kani.snapshot.install``.
    **fields : object
        Bounded values shared by the start and closing events. Never pass a
        path, URL, argument list, digest or payload.

    Yields
    ------
    OperationSpan
        Mutable completion facts for the operation body.
    """
    span_id = f"span-{next(_SPAN_IDS)}"
    started = time.monotonic()
    log_event(logger, f"{event}.start", **{**fields, "span_id": span_id})
    span = OperationSpan()
    try:
        yield span
    except Exception as exc:
        # Only the class name is bounded; the message may carry a path. One
        # mapping means a start field with the same name is overridden rather
        # than raising a duplicate-keyword TypeError after the body succeeded.
        failed = {
            **fields,
            "span_id": span_id,
            "reason": type(exc).__name__,
            "elapsed_seconds": _elapsed_seconds(started),
        }
        log_event(logger, f"{event}.failed", **failed)
        raise
    complete = {
        **fields,
        **span.fields,
        "span_id": span_id,
        "outcome": span.outcome,
        "elapsed_seconds": _elapsed_seconds(started),
    }
    log_event(logger, f"{event}.complete", **complete)
