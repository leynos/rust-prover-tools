"""Deadline checkpoints shared by long snapshot filesystem work.

A phase such as copying, hashing, extraction or tree validation accepts a
callable and invokes it repeatedly, so an installation budget interrupts the
phase instead of being noticed only at its boundary. Callers without a budget
pass `None`; `as_checkpoint` converts that into a no-op so the phase code never
branches on the presence of a deadline.
"""

from __future__ import annotations

import collections.abc as cabc

__all__ = ("Progress", "as_checkpoint", "noop")

#: A callable invoked repeatedly while one long phase runs.
type Progress = cabc.Callable[[], None]


def noop() -> None:
    """Return immediately; the checkpoint for work without a deadline."""


def as_checkpoint(checkpoint: Progress | None) -> Progress:
    """Return `checkpoint`, or a no-op when the caller has no budget."""
    return noop if checkpoint is None else checkpoint
