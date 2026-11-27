"""Open the configured evidence destinations for one supervised run.

This is the one place the library and the CLI turn configured paths into open
sinks, so a destination is opened the same way whichever entry point a caller
used. The write contract is stated by the names: `prepare_jsonl_destination`
and `prepare_and_open_diagnostics` write, because a partial trailing JSONL
record must be completed before the next record can start on its own line;
`open_destinations` and the sink constructors do not write. A caller that
opens sinks directly prepares the boundary itself and passes the resulting
offset. The library owns a file only while the run that writes it is active: a
caller that supplies its own `Diagnostics` keeps ownership of every stream and
file, and this module neither closes nor flushes what it did not open.
"""

from __future__ import annotations

import contextlib
import os
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
    "prepare_and_open_diagnostics",
    "prepare_jsonl_destination",
)


@contextlib.contextmanager
def prepare_and_open_diagnostics(
    config: SupervisionConfig,
    provided: Diagnostics | None,
    *,
    human_stream: typ.TextIO | None = None,
) -> cabc.Generator[Diagnostics]:
    """Prepare the JSONL boundary, then yield the opened diagnostics.

    This function writes: a partial trailing JSONL record is completed by
    `prepare_jsonl_destination` before the sink is opened, so the returned
    JSONL sink starts at the repaired offset. The write is named here rather
    than hidden inside an opener.

    Parameters
    ----------
    config : SupervisionConfig
        Policy whose sink destinations and line bound are used.
    provided : Diagnostics | None
        Caller-owned diagnostics. When supplied, they override the configured
        destinations and remain owned by the caller; this manager neither
        closes nor flushes streams it did not open, and nothing is prepared.
    human_stream : typing.TextIO | None
        Stream a human sink falls back to when no path is configured. `None`
        disables the human sink; the CLI passes standard output.

    Yields
    ------
    Diagnostics
        The provided object unchanged, or sinks opened for the configured
        destinations for the duration of the context.

    Raises
    ------
    ValueError
        Two configured destinations name one file, or the fallback human
        stream and the JSONL destination are the same file.
    OSError
        The JSONL destination cannot be read or repaired.
    """
    require_distinct_destinations(
        human_path=config.sinks.human_path,
        jsonl_path=config.sinks.jsonl_path,
        report_path=config.sinks.report_path,
    )
    if provided is not None:
        yield provided
        return
    jsonl_path = config.sinks.jsonl_path
    if human_stream is not None and jsonl_path is not None:
        _refuse_stream_alias(human_stream, jsonl_path)
    jsonl_start_offset = (
        0 if jsonl_path is None else prepare_jsonl_destination(jsonl_path)
    )
    with contextlib.ExitStack() as stack:
        yield open_destinations(
            stack,
            config.sinks,
            human_stream=human_stream,
            max_line_bytes=config.max_line_bytes,
            jsonl_start_offset=jsonl_start_offset,
        )


#: `fileno` failures that mean a stream has no descriptor at all. Named apart
#: from the `try` so the formatter cannot rewrite it to PEP 758 syntax, which
#: the pinned pylint interpreter cannot parse.
_NO_STREAM_IDENTITY: typ.Final[tuple[type[BaseException], ...]] = (
    AttributeError,
    ValueError,
)


def _refuse_stream_alias(stream: typ.TextIO, path: Path) -> None:
    """Refuse a JSONL destination that is also the human fallback stream.

    A caller that passes its standard output as the human fallback and the
    same file as the JSONL destination would interleave two grammars in one
    handle. A stream with no descriptor, such as a capture object, has no
    identity to compare and is left alone.
    """
    try:
        fileno = stream.fileno()
    except _NO_STREAM_IDENTITY:
        return
    try:
        stream_stat = os.fstat(fileno)
    except OSError:
        return
    try:
        target = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (stream_stat.st_dev, stream_stat.st_ino) == (target.st_dev, target.st_ino):
        msg = f"the human stream already names the JSONL destination: {path}"
        raise ValueError(msg)


def prepare_jsonl_destination(path: Path) -> int:
    """Complete a partial trailing record and return the next byte offset.

    This step writes to `path`: when the destination does not end with a
    newline, one is appended so the first record this run writes starts on its
    own line instead of sharing an unterminated record with the previous run.
    It is deliberately separate from opening, so a reader of `open_destinations`
    can see that opening itself has no write side effect.

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
    `prepare_and_open_diagnostics`; this function only opens. `human_stream`
    is the stream a human sink falls back to when no path is configured, or
    `None` to disable the human sink. A JSONL destination must already have
    been prepared with `prepare_jsonl_destination`, whose result the caller
    passes as `jsonl_start_offset`; opening never repairs the boundary.

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
