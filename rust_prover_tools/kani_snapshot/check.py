"""Validation of the active snapshot generation without installing.

A check takes the identity lock in shared mode and revalidates the pointer,
manifest, receipt, cached payloads, toolchain link and effective compiler under
the caller's deadline. It writes nothing to the snapshot cache, so a failed
check leaves that cache exactly as it found it and the caller can install to
repair. The admission probe is outside that guarantee: it asks the effective
compiler to compile a fixture, so a wrapper the operator has configured may
write to its own caches or build directory while the probe runs.
"""

from __future__ import annotations

import functools
import logging
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import identity_layout, snapshot_layout
from rust_prover_tools.kani_snapshot.install_model import (
    Deadline,
    InstallHooks,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.install_state import (
    active_manifest,
    identity_lock,
    load_active,
    publication_pending,
    validate_generation,
)
from rust_prover_tools.observability import operation_span

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity

__all__ = ("check_snapshot",)

_LOGGER = logging.getLogger(__name__)


def check_snapshot(
    options: InstallOptions,
    hooks: InstallHooks | None = None,
) -> SnapshotIdentity:
    """Validate and report the active generation without installing.

    The pointer is resolved and the generation validated while the identity
    lock is held. A repair publishes its cached payloads file by file under
    that same lock, so reading without it could validate a half-repaired cache
    and report a failure that the running repair is about to resolve. The
    shared reads that establish the pointer are supervised like the lock, so a
    hung filesystem cannot hold it past the deadline.

    Raises
    ------
    ProverToolError
        Raised when no generation is active, the identity lock cannot be
        acquired within its bounded wait, or any receipt, payload, toolchain
        or compiler check fails.
    """
    with operation_span(_LOGGER, "kani.snapshot.check") as span:
        chosen = InstallHooks() if hooks is None else hooks
        deadline = Deadline(time.monotonic() + options.deadline_seconds)
        with identity_lock(
            options,
            timeout=options.lock_timeout_seconds,
            budget=deadline,
            read_only=True,
        ):
            # The pointer is not known yet, and the reads below are
            # identity-level, so no generation name is invented for them.
            layout = identity_layout(options.pin, cache_home=options.cache_home)
            if deadline.supervise(
                "publication state read",
                functools.partial(publication_pending, layout),
            ):
                msg = "snapshot cache publication did not finish; run install to repair"
                raise ProverToolError(msg)
            pointer = deadline.supervise(
                "active pointer read",
                functools.partial(load_active, layout),
            )
            if pointer is None:
                msg = "snapshot is not installed; run install first"
                raise ProverToolError(msg)
            manifest = deadline.supervise(
                "manifest read",
                functools.partial(active_manifest, options.pin, layout),
            )
            generation = snapshot_layout(
                options.pin,
                cache_home=options.cache_home,
                generation=pointer.generation,
            )
            # The compiler probes and filesystem validation must not outlive
            # the budget, so they run under the same supervision as the reads.
            deadline.check("active generation validation")
            identity = deadline.supervise(
                "active generation validation",
                functools.partial(
                    validate_generation,
                    options,
                    manifest,
                    generation,
                    chosen.probe,
                    deadline.remaining,
                    checkpoint=deadline.checkpoint("active generation validation"),
                ),
            )
            # `run_within` returns as soon as its worker has finished, and that
            # can be after the budget expired, so a validation that spent the
            # whole budget must not report a successful check.
            deadline.check("active generation validation")
        span.finish("valid")
        return identity
