"""Readers for the text the opt-in consumer smoke has to interpret.

The CLI renders a generation path unescaped, so it may contain spaces, and a
trace can be larger than the validator's ceiling. Both are read here, beside
each other, so the parsing rules are in one place and each read is bounded.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.kani_snapshot.producer.setup_trace import MAX_TRACE_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "generation_from_output",
    "read_trace",
    "report",
)

#: The CLI renders the generation path unescaped, so it may hold spaces.
_GENERATION: typ.Final[re.Pattern[str]] = re.compile(r"\bat (?P<path>.+)$")


def report(note: str) -> None:
    """Print one bounded evidence note, so a passing run is legible."""
    print(f"[kani-smoke] {note}")


def read_trace(path: Path) -> str:
    """Return a trace, refusing one larger than the documented ceiling."""
    with path.open("rb") as handle:
        data = handle.read(MAX_TRACE_BYTES + 1)
    assert len(data) <= MAX_TRACE_BYTES, (
        f"{path} exceeds the {MAX_TRACE_BYTES}-byte trace ceiling"
    )
    return data.decode("utf-8", errors="replace")


def generation_from_output(text: str) -> str | None:
    """Return the generation path an install or check line names, if any."""
    for line in reversed(text.splitlines()):
        if line.startswith("snapshot "):
            match = _GENERATION.search(line)
            if match is not None:
                return match.group("path")
    return None
