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

__all__ = ("fsync_directory", "write_active")

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
    immediately before the rename. An installation whose budget expires while
    writing or flushing the temporary file therefore leaves the previous
    pointer in place rather than publishing after its deadline. The rename is
    a single atomic operation on the calling thread, so it is issued only
    while the budget remains and can never be completed later by an abandoned
    worker.

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
    # The rename is the point after which the new generation is visible. A
    # failure here still leaves the previous pointer in place, so it is a
    # publication failure and the temporary document is discarded.
    try:
        pending.replace(layout.active_path)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    # Everything below describes durability, not publication: the generation
    # is already active and the message must not suggest otherwise.
    try:
        _flush_identity_directory(layout, budget)
    except ProverToolError as exc:
        msg = f"snapshot generation is active but its activation is not durable: {exc}"
        raise ProverToolError(msg) from exc


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


def _flush_identity_directory(
    layout: SnapshotLayout,
    budget: InstallBudget | None,
) -> None:
    """Make the published rename durable, bounded by `budget`.

    A timeout is raised as `ProverToolError` by `supervise`, and an `OSError`
    from the flush is converted so the caller reports one durability
    contract regardless of which failure occurred.
    """

    def flush() -> None:
        """Flush the identity directory entry."""
        fsync_directory(layout.identity_dir)

    try:
        if budget is None:
            flush()
        else:
            budget.supervise("pointer directory flush", flush)
    except OSError as exc:
        msg = f"failed to flush snapshot activation: {exc}"
        raise ProverToolError(msg) from exc


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so the rename survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
