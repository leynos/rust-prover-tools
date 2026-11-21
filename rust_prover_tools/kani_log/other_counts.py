"""Parse the parenthesized other-result counts in a Kani summary.

Kani may report results that are neither passed nor failed, such as
``(2 undetermined)`` or ``(1 unreachable, 2 undetermined)``. The counts are
kept as ordered pairs so reconciliation can check each kind separately; folding
them into one total would let one kind mask another.
"""

from __future__ import annotations

import re

__all__ = ("parse_other_pairs",)

_PAIR = re.compile(r"\s*(\d+)\s+([a-z]+)\s*")


def parse_other_pairs(text: str | None) -> tuple[tuple[int, str], ...]:
    """Parse a comma-separated list of ``N kind`` pairs.

    Parameters
    ----------
    text : str | None
        Text inside the summary's parentheses, or `None` when absent.

    Returns
    -------
    tuple[tuple[int, str], ...]
        ``(count, kind)`` pairs in log order.

    Raises
    ------
    ValueError
        Raised when one comma-separated part is not a count and a kind.
    """
    if not text:
        return ()
    pairs: list[tuple[int, str]] = []
    for part in text.split(","):
        match = _PAIR.fullmatch(part)
        if match is None:
            msg = f"malformed other-result pair {part!r}"
            raise ValueError(msg)
        pairs.append((int(match[1]), match[2]))
    return tuple(pairs)
