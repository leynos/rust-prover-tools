"""Resolve evidence destinations and refuse two roles that name one file.

Human and JSONL records have different grammars, so appending both to one file
produces a file that is neither; the report is written atomically and would
replace evidence already at its path. Deciding whether two roles collide needs
filesystem identity, so the rule lives here, beside the adapters that open the
destinations, and never inside the policy value object.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("require_distinct_destinations",)


def require_distinct_destinations(
    *,
    human_path: Path | None = None,
    jsonl_path: Path | None = None,
    report_path: Path | None = None,
) -> None:
    """Refuse destinations that name one file under two roles.

    Paths are resolved first, and existing paths are compared by inode, so a
    relative spelling, a symlink, a symlinked parent, or a hard link cannot
    disguise an alias. A path whose identity cannot be determined because
    ``stat`` fails is not reported as an alias here: a missing file or a
    transient failure may still open successfully, so the adapter simply
    attempts the open and lets any real `OSError` surface there. Call this from
    the adapter before the first handle is created.

    Parameters
    ----------
    human_path : Path | None
        Human evidence destination, or `None` when it is disabled.
    jsonl_path : Path | None
        Versioned JSONL destination, or `None` when it is disabled.
    report_path : Path | None
        Final report destination, or `None` when it is disabled.

    Raises
    ------
    ValueError
        Two supplied destinations resolve to the same file.
    """
    supplied = [
        (name, path)
        for name, path in (
            ("human_path", human_path),
            ("jsonl_path", jsonl_path),
            ("report_path", report_path),
        )
        if path is not None
    ]
    seen: list[tuple[str, Path]] = []
    for name, path in supplied:
        resolved = path.resolve()
        for other_name, other_path in seen:
            if resolved == other_path or _same_file(path, other_path):
                msg = (
                    "diagnostic destinations must be distinct: "
                    f"{other_name} and {name} both name {resolved}"
                )
                raise ValueError(msg)
        seen.append((name, resolved))


def _same_file(first: Path, second: Path) -> bool:
    """Whether two existing paths name one inode, as a hard link does."""
    try:
        left = first.stat()
        right = second.stat()
    except OSError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
