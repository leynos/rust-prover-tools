"""Reassembly of the two-line records `strace` writes under concurrency.

A descendant that runs while another is traced makes strace split one call
across an `<unfinished ...>` line and a later `<... resumed>` line. Every
fragment is keyed by the process id strace prints, in either its bare form
(`-o` output) or its `[pid N]` stderr form.

Joining them is what lets the trace validator read a complete argument vector;
without it two concurrent children would look like two unparsable records, and a
truncated capture would look complete.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("join_fragments",)


class FragmentError(ProverToolError):
    """A trace's fragment graph is incomplete or inconsistent."""


#: strace writes a fragment under either a bare PID (`-o`) or `[pid N]` (stderr).
_PID_PREFIX: typ.Final[str] = r"(?:(?:\[pid\s+(?P<pid>[0-9]+)\]|(?P<plain>[0-9]+))\s+)"
_UNFINISHED: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PID_PREFIX}(?P<body>.*)<unfinished \.\.\.>$"
)
_RESUMED: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PID_PREFIX}<\.\.\. \w+ resumed>(?P<body>.*)$"
)


def _pid(match: re.Match[str]) -> str:
    """Return the process id a fragment names, in either prefix form."""
    return match.group("pid") or match.group("plain")


def join_fragments(lines: cabc.Iterable[str]) -> list[str]:
    """Join strace's unfinished/resumed fragments, keyed by process id.

    Concurrent descendants can make strace split one call across two lines.
    A fragment with no matching resume is refused, so a truncated trace is
    never mistaken for a complete one.
    """
    pending: dict[str, str] = {}
    joined: list[str] = []
    for line in lines:
        if not _consume(line, pending, joined):
            joined.append(line)
    _require_no_pending(pending)
    return joined


def _consume(line: str, pending: dict[str, str], joined: list[str]) -> bool:
    """Consume one fragment line, reporting whether it belonged to a fragment."""
    unfinished = _UNFINISHED.match(line)
    if unfinished is not None:
        pid = _pid(unfinished)
        if pid in pending:
            msg = (
                f"setup trace has more than one unfinished record for process "
                f"{pid!r}: {line[:200]!r}"
            )
            raise FragmentError(msg)
        pending[pid] = unfinished.group("body")
        return True
    resumed = _RESUMED.match(line)
    if resumed is not None:
        joined.append(_resume(resumed, pending))
        return True
    return False


def _resume(match: re.Match[str], pending: dict[str, str]) -> str:
    """Return the completed record a resumed fragment forms."""
    pid = _pid(match)
    if pid not in pending:
        msg = f"setup trace resumes an unknown process {pid!r}: {match.string[:200]!r}"
        raise FragmentError(msg)
    tail = match.group("body").lstrip("> ")
    return f"{pid} {pending.pop(pid)}{tail}"


def _require_no_pending(pending: dict[str, str]) -> None:
    """Refuse a trace that ends with an unfinished fragment."""
    if pending:
        msg = f"setup trace has unfinished records: {sorted(pending)}"
        raise FragmentError(msg)
