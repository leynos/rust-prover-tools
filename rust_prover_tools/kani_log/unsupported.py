"""Read Kani's pre-flight list of constructs it cannot model.

The list is one report region with its own entry grammar, so it lives apart
from the block and summary readers that share the rest of ``reports``.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.syntax import UNSUPPORTED_ENTRY, KaniLogError

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.reader import Reader

#: Largest number of unsupported constructs one log retains.
MAX_UNSUPPORTED: typ.Final[int] = 10_000

__all__ = ("unsupported_constructs",)


def unsupported_constructs(reader: Reader) -> list[str]:
    """Consume Kani's pre-flight list of constructs it cannot model.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned at the list's heading line.

    Returns
    -------
    list[str]
        One name per listed construct, in source order.  The heading and the
        entries are behind the cursor; the first non-entry line is left for
        the caller.

    Raises
    ------
    KaniLogError
        Never directly; a truncated list simply ends at the last entry.
    """
    reader.take()
    names: list[str] = []
    while not reader.exhausted:
        entry = UNSUPPORTED_ENTRY.match(reader.peek())
        if entry is None:
            if reader.peek().strip():
                return names
            reader.take()
            continue
        if len(names) >= MAX_UNSUPPORTED:
            msg = (
                f"{reader.where()}: the unsupported list names more than "
                f"{MAX_UNSUPPORTED} constructs"
            )
            raise KaniLogError(msg)
        names.append(entry["name"])
        reader.take()
    return names
