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
import re
import string
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


#: One C escape strace may write inside a quoted string.
_C_ESCAPE: typ.Final[re.Pattern[str]] = re.compile(
    r"\\(?:[0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)", re.DOTALL
)
#: C escapes whose meaning differs from the character they name.
_C_ESCAPE_VALUES: typ.Final[dict[str, str]] = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
#: An octal escape holds a byte, and strace writes non-ASCII text that way.
_OCTAL_DIGITS: typ.Final[frozenset[str]] = frozenset(string.octdigits)
_MAX_BYTE: typ.Final[int] = 0xFF


def _unescape(value: str) -> str:
    r"""Decode one strace-quoted argument, refusing text strace cannot encode.

    Strace quotes a C string, not a JSON string: it writes a non-ASCII byte as
    an octal escape (`caf\303\251` for `café`), which `json.loads` rejects.
    Decoding must follow strace's own format, or a valid capture under such a
    path would be refused and the clean-setup check would fail on a correct
    installation.

    Because strace writes the argument's bytes, the escapes are accumulated as
    bytes and the whole value is decoded as UTF-8 once. Every escape is
    consumed in one pass and an unrecognized one is refused rather than passed
    through as raw text: a policy check reading undecoded text could then
    compare a different argument from the one that ran.
    """
    raw = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            raw += character.encode("utf-8")
            index += 1
            continue
        match = _C_ESCAPE.match(value, index)
        if match is None:
            msg = f"setup trace has an argument strace cannot quote: {value[:120]!r}"
            raise SetupTraceError(msg)
        raw.append(_escaped_byte(match.group(), value))
        index = match.end()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"setup trace has an argument that is not UTF-8: {value[:120]!r}"
        raise SetupTraceError(msg) from exc


def _escaped_byte(escape: str, whole: str) -> int:
    """Return the single byte one complete C escape denotes."""
    body = escape[1:]
    if body[:1] in {"x", "X"}:
        if len(body) == 1:
            # strace never writes a bare `\x`; it means the capture is damaged.
            msg = f"setup trace has a malformed escape: {whole[:120]!r}"
            raise SetupTraceError(msg)
        value = int(body[1:], 16)
    elif body[:1] in _OCTAL_DIGITS:
        value = int(body, 8)
    elif body[:1].isdigit():
        # `\8` and `\9` are not octal escapes; reading them as base 8 would
        # raise `ValueError` from inside the parser.
        msg = f"setup trace has a malformed escape: {whole[:120]!r}"
        raise SetupTraceError(msg)
    elif body in _C_ESCAPE_VALUES:
        return ord(_C_ESCAPE_VALUES[body])
    else:
        # `\\` and `\"` denote themselves; any other escaped character is the
        # character, which is how a C string treats an unrecognized escape.
        encoded = body.encode("utf-8")
        if len(encoded) != 1:
            msg = f"setup trace has a multi-byte escape: {whole[:120]!r}"
            raise SetupTraceError(msg)
        return encoded[0]
    if value > _MAX_BYTE:
        msg = f"setup trace has an out-of-range escape: {whole[:120]!r}"
        raise SetupTraceError(msg)
    return value


def _argv(value: str) -> tuple[str, ...]:
    """Return the argument vector from strace's bracketed list.

    Every token of the field must be a quoted argument. `re.findall` alone
    would skip an unconsumed token, so `["rustc", @compile.args]` would read as
    the single argument `rustc` and the response-file check would never see it.
    The field is therefore split on commas at top level and each token must
    match exactly.

    strace elides a long string as `"prefix"...` under its default string
    limit, which could hide a `.rs` suffix from the code-generation check. An
    elided argument is therefore refused rather than silently truncated.
    """
    if _ELIDED.search(value) is not None or _OMITTED.search(value) is not None:
        msg = f"strace elided an argument, so it cannot be checked: {value[:120]!r}"
        raise SetupTraceError(msg)
    return tuple(_unescape(item) for item in _argument_tokens(value))


def _argument_tokens(value: str) -> list[str]:
    """Return each quoted argument's undecoded body, refusing anything else.

    The field is a comma-separated list, so the grammar is `token ("," token)*`
    with optional spaces around each comma. A missing, doubled, leading or
    trailing comma is a damaged capture: strace writes exactly one comma
    between arguments, so anything else means a token was lost or merged.
    """
    tokens: list[str] = []
    index = 0
    length = len(value)
    first = True
    while index < length:
        if not first:
            index = _require_comma(value, index)
            if index >= length:
                msg = f"setup trace has an unreadable argument list: {value[:120]!r}"
                raise SetupTraceError(msg)
        index = _skip_spaces(value, index)
        if index >= length:
            break
        match = _ARGUMENT.match(value, index)
        if match is None:
            msg = f"setup trace has an unreadable argument list: {value[:120]!r}"
            raise SetupTraceError(msg)
        tokens.append(match.group(1))
        index = match.end()
        first = False
    return tokens


def _require_comma(value: str, index: int) -> int:
    """Require exactly one comma, surrounded by optional spaces."""
    index = _skip_spaces(value, index)
    if index >= len(value) or value[index] != ",":
        msg = f"setup trace has an unreadable argument list: {value[:120]!r}"
        raise SetupTraceError(msg)
    index += 1
    index = _skip_spaces(value, index)
    if index < len(value) and value[index] == ",":
        msg = f"setup trace has an unreadable argument list: {value[:120]!r}"
        raise SetupTraceError(msg)
    return index


def _skip_spaces(value: str, index: int) -> int:
    """Return the index of the first non-space character at or after `index`."""
    while index < len(value) and value[index] == " ":
        index += 1
    return index


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
