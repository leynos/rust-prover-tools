"""Open the configured evidence destinations for one supervised run.

The library owns a file only while the run that writes it is active. A caller
that supplies its own `Diagnostics` keeps ownership of every stream and file;
this module neither closes nor flushes what it did not open.
"""

from __future__ import annotations

import contextlib
import typing as typ

from rust_prover_tools.supervision import (
    Diagnostics,
    HumanSink,
    JsonlSink,
    NullSink,
    SupervisionConfig,
    jsonl_append_offset,
    require_distinct_destinations,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("open_diagnostics",)


@contextlib.contextmanager
def open_diagnostics(
    config: SupervisionConfig,
    provided: Diagnostics | None,
) -> cabc.Generator[Diagnostics]:
    """Yield the configured diagnostics, closing any files this call opened.

    Parameters
    ----------
    config : SupervisionConfig
        Policy whose sink destinations and line bound are used.
    provided : Diagnostics | None
        Caller-owned diagnostics. When supplied, they override the configured
        destinations and remain owned by the caller; this manager neither
        closes nor flushes streams it did not open.

    Yields
    ------
    Diagnostics
        The provided object unchanged, or sinks opened for the configured
        destinations for the duration of the context.
    """
    if provided is not None:
        yield provided
        return
    with contextlib.ExitStack() as stack:
        yield _open(stack, config)


def _open(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
) -> Diagnostics:
    """Open the configured sink destinations for one run.

    Library callers reach the filesystem here, so the inode-aware alias rule
    runs here before the first handle exists; the CLI checks the same rule
    before it hands over its own destinations.
    """
    require_distinct_destinations(
        human_path=config.sinks.human_path,
        jsonl_path=config.sinks.jsonl_path,
        report_path=config.sinks.report_path,
    )
    human = _human_sink(stack, config)
    jsonl = _jsonl_sink(stack, config)
    return Diagnostics(human=human, jsonl=jsonl)


def _human_sink(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
) -> HumanSink | NullSink:
    """Open the configured human destination, if any."""
    path = config.sinks.human_path
    if path is None:
        return NullSink()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = stack.enter_context(path.open("a", encoding="utf-8", newline=""))
    return HumanSink(handle, max_line_bytes=config.max_line_bytes)


def _jsonl_sink(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
) -> JsonlSink | NullSink:
    """Open the configured JSONL destination, if any."""
    path = config.sinks.jsonl_path
    if path is None:
        return NullSink()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = jsonl_append_offset(path)
    handle = stack.enter_context(path.open("a", encoding="utf-8", newline=""))
    return JsonlSink(
        handle,
        path=path,
        max_line_bytes=config.max_line_bytes,
        start_offset=existing,
    )
