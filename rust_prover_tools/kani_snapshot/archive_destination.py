"""Destination preparation for publishing an extracted snapshot tree.

Extraction publishes by renaming a fully written private sibling into place.
That is only safe when the destination is absent or empty and no ancestor can
be swapped by another user, so both rules are checked here, before any byte is
written, and the private staging path is chosen from a single place.
"""

from __future__ import annotations

import os
import stat
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("PRIVATE_DIRECTORY_MODE", "prepare_destination")

#: Staging content is owner-only until the rename publishes it.
PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700

_WORLD_WRITABLE: typ.Final[int] = 0o022
_STICKY_BIT: typ.Final[int] = stat.S_ISVTX


def prepare_destination(destination: Path) -> Path:
    """Validate `destination` and return a fresh private staging path.

    Parameters
    ----------
    destination : Path
        Final extraction root. It must not exist, or be an empty directory,
        and its ancestors must not be attacker-writable outside a sticky
        directory such as `/tmp`.

    Returns
    -------
    Path
        A sibling path, unique to this process and moment, that the caller
        creates and later renames onto `destination`.

    Raises
    ------
    ProverToolError
        Raised when the destination would merge with existing content, an
        ancestor is unsafe, or the destination cannot be inspected.
    """
    _require_absent_or_empty(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _require_private_ancestors(destination.parent)
    return destination.with_name(
        f".{destination.name}.extract-{os.getpid()}-{time.monotonic_ns()}"
    )


def _require_absent_or_empty(destination: Path) -> None:
    """Reject a destination that would merge with existing content."""
    try:
        if not destination.exists():
            return
        if not destination.is_dir():
            msg = f"snapshot extraction destination is not a directory: {destination}"
            raise ProverToolError(msg)
        if any(destination.iterdir()):
            msg = f"snapshot extraction destination must be empty: {destination}"
            raise ProverToolError(msg)
    except OSError as exc:
        msg = f"failed to inspect snapshot extraction destination: {exc}"
        raise ProverToolError(msg) from exc


def _require_private_ancestors(start: Path) -> None:
    """Reject an attacker-writable ancestor outside a sticky directory."""
    current = start
    for parent in (current, *current.parents):
        try:
            status = parent.stat()
        except OSError as exc:
            msg = f"failed to inspect extraction ancestor '{parent}': {exc}"
            raise ProverToolError(msg) from exc
        if not stat.S_ISDIR(status.st_mode):
            msg = f"snapshot extraction ancestor is not a directory: {parent}"
            raise ProverToolError(msg)
        if status.st_mode & _WORLD_WRITABLE and not status.st_mode & _STICKY_BIT:
            msg = f"snapshot extraction ancestor is attacker-writable: {parent}"
            raise ProverToolError(msg)
