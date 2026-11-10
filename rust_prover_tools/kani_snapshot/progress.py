"""Deadline checkpoints and bounded blocking work for snapshot operations.

A phase such as copying, hashing, extraction or tree validation accepts a
callable and invokes it repeatedly, so an installation budget interrupts the
phase instead of being noticed only at its boundary. Callers without a budget
pass `None`; `as_checkpoint` converts that into a no-op so the phase code never
branches on the presence of a deadline.

A checkpoint is only reached between blocking calls. A read, write or `fsync`
that stalls on a hung filesystem never reaches one, so `run_within` runs such
an operation in a private thread while its caller waits no longer than the
budget that remains.
"""

from __future__ import annotations

import collections.abc as cabc
import threading
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = ("Progress", "as_checkpoint", "noop", "run_within")

#: A callable invoked repeatedly while one long phase runs.
type Progress = cabc.Callable[[], None]


def noop() -> None:
    """Return immediately; the checkpoint for work without a deadline."""


def as_checkpoint(checkpoint: Progress | None) -> Progress:
    """Return `checkpoint`, or a no-op when the caller has no budget."""
    return noop if checkpoint is None else checkpoint


def run_within[T](
    operation: cabc.Callable[[], T],
    *,
    remaining: cabc.Callable[[], float],
    phase: str,
) -> T:
    """Run one blocking local operation without exceeding the budget.

    A stalled `read`, `write` or `fsync` cannot observe a cooperative
    checkpoint, so the operation runs in a private daemon thread while this
    caller waits only for the seconds that remain. On expiry the caller raises
    a deadline error and abandons the worker.

    Abandonment is only safe for work whose late completion cannot change a
    decision another operation has already made. That covers operations on a
    private staging directory or an unreferenced generation, reads, removal
    of a file this run owns by name, and shared writes whose published name is
    unique or totally ordered, such as an activation record that must be
    created at a sequence number a later activation has already passed. A
    shared write that replaces one file qualifies only while a late value
    cannot retire an unfinished marker, as with the completion watermark: an
    installation links its activation record before recording completion, so a
    late watermark ranks no higher than the base any publication that began
    after that link observed, and only a completion above that base retires a
    marker. Callers pass anything else a budget check immediately before it
    happens, or keep it on this thread.
    """
    budget = remaining()
    if budget <= 0:
        msg = f"snapshot installation deadline expired during {phase}"
        raise ProverToolError(msg)
    outcome: list[tuple[bool, object]] = []

    def run() -> None:
        """Run the operation and carry its result or failure back."""
        try:
            outcome.append((True, operation()))
        except Exception as exc:  # ruff: ignore[blind-except] - transported
            outcome.append((False, exc))

    worker = threading.Thread(target=run, name=f"snapshot {phase}", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        msg = f"snapshot installation deadline expired during {phase}"
        raise ProverToolError(msg)
    succeeded, value = outcome[0]
    if succeeded:
        return typ.cast("T", value)
    raise typ.cast("Exception", value)
