"""Bounded filesystem flushes shared by snapshot installation.

Durability is the part of the cache contract that a successful status must not
overstate. A directory entry can be flushed, a file's bytes and metadata can be
flushed, and an activation record counts as durable only once both the
directory holding it and the identity directory holding that directory are
flushed. Each helper accepts the remaining installation budget, so a flush
stalled on a hung filesystem cannot hold the identity lock past the deadline,
and each reports a failure against a bounded name rather than a whole private
path.
"""

from __future__ import annotations

import os
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = (
    "confirm_activation",
    "flush_directory",
    "fsync_directory",
    "fsync_file",
)


def confirm_activation(
    layout: SnapshotLayout,
    budget: InstallBudget | None = None,
) -> None:
    """Make a published activation record durable, or say it is unconfirmed.

    The record lives in the activation directory, whose own entry lives in the
    identity directory, so both must be flushed before activation counts as
    durable. Reuse reissues these flushes: a previous activation may have
    published its record and then failed or timed out while flushing, and a
    retry must not report success until the record is confirmed durable.
    """
    try:
        flush_directory(
            layout.activations_dir,
            budget=budget,
            phase="pointer directory flush",
        )
        flush_directory(
            layout.identity_dir,
            budget=budget,
            phase="activation directory flush",
        )
    except ProverToolError as exc:
        msg = f"snapshot generation is active but its activation is not durable: {exc}"
        raise ProverToolError(msg) from exc


def flush_directory(
    path: Path,
    *,
    budget: InstallBudget | None = None,
    phase: str = "directory flush",
) -> None:
    """Flush one directory entry, bounded by `budget` when one is supplied.

    A supervised flush that outlives the budget raises the deadline error from
    `supervise`. An `OSError` is converted so every caller reports the same
    durability contract, while the phase name identifies which directory
    entry could not be confirmed.
    """

    def flush() -> None:
        """Flush one directory entry."""
        fsync_directory(path)

    try:
        if budget is None:
            flush()
        else:
            budget.supervise(phase, flush)
    except OSError as exc:
        msg = f"failed to flush snapshot directory '{path.name}': {exc}"
        raise ProverToolError(msg) from exc


def fsync_file(
    path: Path,
    *,
    budget: InstallBudget | None = None,
    phase: str = "file flush",
) -> None:
    """Flush one file's content and metadata, bounded by `budget`.

    Required executables are written by setup or copied by the installer, so
    flushing their directory entry alone would leave the bytes a crash could
    lose. A failure is reported against the file's name rather than its whole
    path, which keeps the message bounded and private.
    """

    def flush() -> None:
        """Flush one file's content and metadata."""
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    try:
        if budget is None:
            flush()
        else:
            budget.supervise(phase, flush)
    except OSError as exc:
        msg = f"failed to flush snapshot file '{path.name}': {exc}"
        raise ProverToolError(msg) from exc


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so the rename survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
