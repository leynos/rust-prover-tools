"""Durable publication of the active-generation pointer.

The pointer is the only mutable shared state in the cache. It is replaced
atomically under the identity lock, so a reader sees either the previous
generation or the complete new one. Publication is also gated on the remaining
installation budget immediately before the rename, so an installation that ran
out of time cannot become visible after its deadline.
"""

from __future__ import annotations

import json
import logging
import os
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    is_safe_generation_name,
)
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = ("fsync_directory", "write_active")

_LOGGER = logging.getLogger(__name__)
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
    pointer in place rather than publishing after its deadline. The directory
    flush that follows the rename cannot change what is published, so a stall
    there is reported as deferred durability instead of as a failed
    activation.
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
        pending.replace(layout.active_path)
        _flush_identity_directory(layout, budget)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        pending.unlink(missing_ok=True)
        raise


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
    """Flush the rename, reporting a stall after publication as deferred."""
    if budget is None:
        fsync_directory(layout.identity_dir)
        return
    try:
        budget.supervise(
            "pointer directory flush",
            lambda: fsync_directory(layout.identity_dir),
        )
    except ProverToolError:
        # The rename is already visible to readers. Only crash durability is
        # deferred to the operating system, so this must not fail an
        # activation that has happened.
        log_event(_LOGGER, "kani.install.activation_flush_deferred")


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so the rename survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
