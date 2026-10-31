"""Opt-in structured events for operational boundaries.

The library installs no logging handlers and emits nothing by default. An
application opts in by lowering the level of the relevant module logger, for
example ``logging.getLogger("rust_prover_tools").setLevel(logging.DEBUG)``.
Each event carries a bounded ``event_data`` mapping on the log record so a
collector can read fields without parsing the message text.
"""

from __future__ import annotations

import typing as typ

__all__ = ("log_event",)

if typ.TYPE_CHECKING:
    # Annotations are strings, so the module is needed only for type checking.
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
