"""Open the configured evidence destinations for one supervised run.

This is the one place the library and the CLI turn configured paths into open
sinks, so a destination is opened the same way whichever entry point a caller
used. Opening never writes: a JSONL destination whose final record is partial
is completed by `prepare_jsonl_destination`, an explicit, named step each
entry point runs before it opens anything. The library owns a file only while
the run that writes it is active: a caller that supplies its own `Diagnostics`
keeps ownership of every stream and file, and this module neither closes nor
flushes what it did not open.
"""

from __future__ import annotations

import contextlib
import typing as typ

from rust_prover_tools.supervision import (
    Diagnostics,
    DiagnosticSinks,
    HumanSink,
    JsonlSink,
    NullSink,
    SupervisionConfig,
    repair_jsonl_boundary,
    require_distinct_destinations,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "open_destinations",
    "open_diagnostics",
    "prepare_jsonl_destination",
)


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
    require_distinct_destinations(
        human_path=config.sinks.human_path,
        jsonl_path=config.sinks.jsonl_path,
        report_path=config.sinks.report_path,
    )
    jsonl_path = config.sinks.jsonl_path
    jsonl_start_offset = (
        0 if jsonl_path is None else prepare_jsonl_destination(jsonl_path)
    )
    with contextlib.ExitStack() as stack:
        yield open_destinations(
            stack,
            config.sinks,
            human_stream=None,
            max_line_bytes=config.max_line_bytes,
            jsonl_start_offset=jsonl_start_offset,
        )


def prepare_jsonl_destination(path: Path) -> int:
    """Complete a partial trailing record and return the next byte offset.

    This step writes to `path`: when the destination does not end with a
    newline, one is appended so the first record this run writes starts on its
    own line instead of sharing an unterminated record with the previous run.
    It is deliberately separate from opening, so a reader of the opening code
    can see that opening has no write side effect.

    Parameters
    ----------
    path : Path
        JSONL destination to prepare.

    Returns
    -------
    int
        Byte offset at which the next record begins.

    Raises
    ------
    OSError
        The destination cannot be read or repaired.
    """
    return repair_jsonl_boundary(path)


# The offset is opening policy, so the keyword set stays flat rather than
# growing a bundle whose only member is passed in by the two entry points.
# pylint: disable-next=too-many-arguments
def open_destinations(  # ruff: ignore[too-many-arguments] - one flat policy
    stack: contextlib.ExitStack,
    sinks: DiagnosticSinks,
    *,
    human_stream: typ.TextIO | None,
    max_line_bytes: int,
    jsonl_start_offset: int = 0,
) -> Diagnostics:
    """Open the configured human and JSONL sinks for one run.

    The caller is responsible for the alias rule when it has not gone through
    `open_diagnostics`; this function only opens. `human_stream` is the stream
    a human sink falls back to when no path is configured, or `None` to
    disable the human sink. A JSONL destination must already have been
    prepared with `prepare_jsonl_destination`, whose result the caller passes
    as `jsonl_start_offset`; opening never repairs the boundary.

    Parameters
    ----------
    stack : contextlib.ExitStack
        Stack that owns every handle opened here.
    sinks : DiagnosticSinks
        Configured destinations.
    human_stream : typing.TextIO | None
        Stream the human sink writes to when no path is set.
    max_line_bytes : int
        Bound applied to one diagnostic line.
    jsonl_start_offset : int
        Byte offset at which the JSONL sink's first record begins.

    Returns
    -------
    Diagnostics
        The opened human and JSONL sinks.
    """
    human = _human_sink(stack, sinks.human_path, human_stream, max_line_bytes)
    jsonl = _jsonl_sink(
        stack,
        sinks.jsonl_path,
        max_line_bytes,
        start_offset=jsonl_start_offset,
    )
    return Diagnostics(human=human, jsonl=jsonl)


def _human_sink(
    stack: contextlib.ExitStack,
    path: Path | None,
    stream: typ.TextIO | None,
    max_line_bytes: int,
) -> HumanSink | NullSink:
    """Open the human destination, or use its configured stream."""
    if path is None:
        if stream is None:
            return NullSink()
        return HumanSink(stream, max_line_bytes=max_line_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = stack.enter_context(path.open("a", encoding="utf-8", newline=""))
    return HumanSink(handle, max_line_bytes=max_line_bytes)


def _jsonl_sink(
    stack: contextlib.ExitStack,
    path: Path | None,
    max_line_bytes: int,
    *,
    start_offset: int,
) -> JsonlSink | NullSink:
    """Open the JSONL destination; the caller prepared its boundary."""
    if path is None:
        return NullSink()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = stack.enter_context(path.open("a", encoding="utf-8", newline=""))
    return JsonlSink(
        handle,
        path=path,
        max_line_bytes=max_line_bytes,
        start_offset=start_offset,
    )
