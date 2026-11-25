"""Reading one bounded `strace -f -e trace=execve` log.

This module is the format half of the clean-setup evidence: it turns strace's
own text into `ExecRecord` values and refuses anything it cannot read exactly.
It knows nothing about which commands are allowed — that policy lives in
`setup_trace`, which imports this module.

The distinction matters because strace's format changes with its own options,
while the policy changes with what setup must not do. Keeping them apart means
a new strace spelling touches only the reader.
"""

from __future__ import annotations

import dataclasses as dc
import json
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.trace_fragments import (
    FragmentError,
    join_fragments,
)

__all__ = (
    "MAX_EXEC_RECORDS",
    "MAX_TRACE_BYTES",
    "ExecRecord",
    "SetupTraceError",
    "basename",
    "parse_exec_trace",
)

#: Largest trace accepted. A real setup trace is a few thousand short lines.
MAX_TRACE_BYTES: typ.Final[int] = 8 * 1024 * 1024

#: Most `execve` records accepted, so a hostile trace cannot exhaust memory.
MAX_EXEC_RECORDS: typ.Final[int] = 10_000

_EXECVE: typ.Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:\[pid\s+[0-9]+\]\s+)|(?:[0-9]+\s+))?"
    r'execve\("(?P<path>(?:[^"\\]|\\.)*)",\s*\[(?P<argv>.*)\],\s*'
    # strace writes the environment as a pointer, optionally annotated with its
    # variable count, or as the whole variable list under `-v`.
    r"(?:0x[0-9a-f]+(?:\s*/\*\s*[0-9]+\s+vars?\s*\*/)?|\[[^\]]*\]|[0-9]+)?\s*\)"
)
_ARGUMENT: typ.Final[re.Pattern[str]] = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ELIDED: typ.Final[re.Pattern[str]] = re.compile(r'"\s*\.\.\.')
#: strace writes a standalone `...` when it omits trailing argument entries.
_OMITTED: typ.Final[re.Pattern[str]] = re.compile(r"(?:^|[,\[])\s*\.\.\.\s*(?:,|$|\])")


class SetupTraceError(ProverToolError):
    """A setup trace shows a forbidden action or no usable evidence."""


@dc.dataclass(frozen=True, slots=True)
class ExecRecord:
    """One `execve` record from a descendant-process trace.

    Attributes
    ----------
    executable : str
        Absolute path strace reported.
    argv : tuple of str
        Argument vector, including the program name.
    """

    executable: str
    argv: tuple[str, ...]

    @property
    def name(self) -> str:
        """The basename of the executable, or of `argv[0]`."""
        return basename(self.executable) or (
            basename(self.argv[0]) if self.argv else ""
        )


def basename(value: str) -> str:
    """Return the final path component without importing `os.path`."""
    return value.rsplit("/", 1)[-1]


def _unescape(value: str) -> str:
    """Decode one strace-quoted argument, refusing text strace cannot encode.

    strace quotes a C string, so a value that is not valid JSON-in-a-string is
    a capture this reader does not understand. Returning the raw text would let
    an odd escape sequence pass through as if it had been decoded, and a policy
    check on that text could then read a different argument than the one that
    ran. The refusal is explicit for that reason.
    """
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError as exc:
        msg = f"setup trace has an argument strace cannot quote: {value[:120]!r}"
        raise SetupTraceError(msg) from exc
    if not isinstance(decoded, str):  # pragma: no cover - json.loads returns str
        msg = f"setup trace has a non-string argument: {value[:120]!r}"
        raise SetupTraceError(msg)
    return decoded


def _argv(value: str) -> tuple[str, ...]:
    """Return the argument vector from strace's bracketed list.

    strace elides a long string as `"prefix"...` under its default string
    limit, which could hide a `.rs` suffix from the code-generation check. An
    elided argument is therefore refused rather than silently truncated.
    """
    if _ELIDED.search(value) is not None or _OMITTED.search(value) is not None:
        msg = f"strace elided an argument, so it cannot be checked: {value[:120]!r}"
        raise SetupTraceError(msg)
    return tuple(_unescape(item) for item in _ARGUMENT.findall(value))


def parse_exec_trace(trace: str) -> tuple[ExecRecord, ...]:
    """Return every `execve` record in a bounded strace trace.

    Parameters
    ----------
    trace : str
        Output of `strace -f -e trace=execve`, to a file or standard error.

    Returns
    -------
    tuple of ExecRecord
        Records in trace order. A line with no `execve` is ignored, and a
        failed `execve` is still a record because the attempt is the evidence.

    Raises
    ------
    SetupTraceError
        Raised when the trace is not text, exceeds the byte ceiling, holds
        more records than the record ceiling, has unbalanced or duplicated
        unfinished fragments, or has an `execve` line it cannot read exactly.
    """
    _require_bounded_trace(trace)
    records: list[ExecRecord] = []
    try:
        joined = join_fragments(trace.splitlines())
    except FragmentError as exc:
        raise SetupTraceError(str(exc)) from exc
    for line in joined:
        record = _record(line)
        if record is not None:
            records.append(record)
        if len(records) > MAX_EXEC_RECORDS:
            msg = f"setup trace exceeds the {MAX_EXEC_RECORDS}-record ceiling"
            raise SetupTraceError(msg)
    return tuple(records)


def _require_bounded_trace(trace: str) -> None:
    """Require a text trace within the documented byte ceiling."""
    if not isinstance(trace, str):
        msg = "setup trace must be text"
        raise SetupTraceError(msg)
    if len(trace) > MAX_TRACE_BYTES:
        msg = f"setup trace exceeds the {MAX_TRACE_BYTES}-byte ceiling"
        raise SetupTraceError(msg)


def _record(line: str) -> ExecRecord | None:
    """Return the record one trace line states, or `None` for a non-execve line."""
    match = _EXECVE.match(line)
    if match is None:
        if "execve(" in line:
            msg = f"setup trace has an unparsable execve record: {line[:200]!r}"
            raise SetupTraceError(msg)
        return None
    return ExecRecord(
        executable=_unescape(match.group("path")),
        argv=_argv(match.group("argv")),
    )
