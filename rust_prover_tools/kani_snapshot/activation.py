"""Durable publication of the active-generation pointer.

The pointer is the only mutable shared state in the cache. It is replaced
atomically under the identity lock, so a reader sees either the previous
generation or the complete new one. Publication is gated on the remaining
installation budget immediately before the rename, and the rename itself is
issued on the calling thread: a supervised worker that timed out could
otherwise complete it after the installation had already reported failure.
Durability of the rename is part of activation, so a flush that cannot be
confirmed is reported as such rather than silently deferred.
"""

from __future__ import annotations

import functools
import json
import os
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    is_safe_generation_name,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = ("flush_directory", "fsync_directory", "write_active")

_PRIVATE_FILE_MODE: typ.Final[int] = 0o600


def write_active(
    layout: SnapshotLayout,
    generation: str,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Atomically replace the active pointer with one generation name.

    The pointer bytes are fsynced before the rename and the identity
    directory is fsynced afterwards, so a crash exposes either the old pointer
    or the new one, never a partial document.

    A supplied budget is checked after the temporary document is durable and
    immediately before the rename, and the rename itself is supervised. An
    installation whose budget expires while writing or flushing the temporary
    file therefore leaves the previous pointer in place rather than publishing
    after its deadline. A rename that outlives the budget is reported as an
    unknown outcome: an abandoned worker may still complete it, so neither
    "published" nor "not published" can be asserted. The pointer document is
    left in place for that worker, and a later `check` or `install` observes
    whichever pointer is active.

    The directory flush that follows the rename is what makes the activation
    survive a crash. It is bounded by the remaining budget, and a flush that
    times out or fails raises a distinct error naming the generation that is
    already visible, because reporting a publication failure would be untrue
    and reporting a durable activation would be unverified.
    """
    if not is_safe_generation_name(generation):
        msg = f"snapshot generation name is not one safe component: {generation!r}"
        raise ProverToolError(msg)
    encoded = json.dumps(
        {"schema_version": SCHEMA_VERSION, "generation": generation},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    pending = layout.active_path.with_name(
        f".active-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        _write_pending_pointer(pending, encoded, budget)
        if budget is not None:
            budget.check("pointer publication")
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        pending.unlink(missing_ok=True)
        raise
    # The rename is the point after which the new generation is visible. An
    # `OSError` still leaves the previous pointer in place, so it is a
    # publication failure and the temporary document is discarded.
    try:
        _publish_pointer(pending, layout.active_path, budget)
    except ProverToolError as exc:
        # The supervised rename may still complete after the caller stops
        # waiting, so the temporary document is kept for it. Reporting either
        # state as fact would be wrong; a later observation resolves it.
        msg = (
            "snapshot activation outcome is unknown: the deadline expired "
            "while publishing the active pointer, which a later check reports"
        )
        raise ProverToolError(msg) from exc
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    # Everything below describes durability, not publication: the generation
    # is already active and the message must not suggest otherwise.
    try:
        flush_directory(
            layout.identity_dir,
            budget=budget,
            phase="pointer directory flush",
        )
    except ProverToolError as exc:
        msg = f"snapshot generation is active but its activation is not durable: {exc}"
        raise ProverToolError(msg) from exc


def _publish_pointer(
    pending: Path,
    target: Path,
    budget: InstallBudget | None,
) -> None:
    """Rename the pending pointer document onto the active path.

    A budget supervises the rename so a stalled filesystem cannot hold the
    caller past the deadline. Supervision is safe only in the sense that the
    caller stops waiting; the rename itself is atomic, so an abandoned worker
    can complete it but can never expose a partial document.
    """
    replace = functools.partial(pending.replace, target)
    if budget is None:
        replace()
    else:
        budget.supervise("pointer publication", replace)


def _write_pending_pointer(
    pending: Path,
    encoded: bytes,
    budget: InstallBudget | None,
) -> None:
    """Write and fsync one private pointer document, bounded by `budget`."""

    def write() -> None:
        """Create the private document and make its bytes durable."""
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            _PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    if budget is None:
        write()
        return
    budget.supervise("pointer write", write)


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


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so the rename survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
