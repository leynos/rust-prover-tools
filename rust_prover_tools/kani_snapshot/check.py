"""Validation of the active snapshot generation without installing.

The module separates a read-only query from effectful compiler verification.
`check_snapshot` takes the identity lock in shared mode and validates the
active pointer, manifest, receipt, cached payloads, installed tree and
toolchain bytes. It starts no process, so it writes nothing to the snapshot
cache and cannot write through a configured compiler wrapper.

`check_active_snapshot` composes that query with
`rust_prover_tools.kani_snapshot.install_state.verify_effective_compiler`,
which runs the effective `rustc -vV` and `kani-compiler --version` through the
configured admission chain. The shared lock stays held across both steps, so a
repair cannot swap the generation between validating its bytes and proving its
compiler. The probe sits outside the write-free guarantee: a wrapper the
operator has configured may write to its own caches or build directory while
it runs.
"""

from __future__ import annotations

import contextlib
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
)
from rust_prover_tools.kani_snapshot.install_validation import (
    ValidatedGeneration,
    validate_generation,
    verify_effective_compiler,
)
from rust_prover_tools.observability import operation_span

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity

__all__ = ("check_active_snapshot", "check_snapshot")

_LOGGER = logging.getLogger(__name__)


def check_snapshot(options: InstallOptions) -> ValidatedGeneration:
    """Validate the active generation's files without running any process.

    The pointer is resolved and the generation validated while the identity
    lock is held. A repair publishes its cached payloads file by file under
    that same lock, so reading without it could validate a half-repaired cache
    and report a failure that the running repair is about to resolve. The
    shared reads that establish the pointer are supervised like the lock, so a
    hung filesystem cannot hold it past the deadline.

    This function does not prove the effective compiler; call
    `check_active_snapshot`, or run `verify_effective_compiler` on the result,
    when that proof is required.

    Raises
    ------
    ProverToolError
        Raised when no generation is active, the identity lock cannot be
        acquired within its bounded wait, or any receipt, payload, toolchain
        or tree check fails.
    """
    with (
        operation_span(_LOGGER, "kani.snapshot.check") as span,
        _validated_under_lock(options) as (validated, _),
    ):
        span.finish("valid")
        return validated


def check_active_snapshot(
    options: InstallOptions,
    hooks: InstallHooks | None = None,
) -> SnapshotIdentity:
    """Validate the active generation and prove its effective compiler.

    The filesystem query and the compiler probe run under one shared lock and
    one deadline, so the generation whose bytes were validated is the
    generation whose compiler is proved. Use `check_snapshot` alone when only
    the installed bytes need checking.

    Parameters
    ----------
    options : InstallOptions
        Reviewed pin, cache root and budgets.
    hooks : InstallHooks | None
        External operations; the probe is the only one used. `None` selects
        the host defaults.

    Returns
    -------
    SnapshotIdentity
        The validated generation's identity report.

    Raises
    ------
    ProverToolError
        Raised when no generation is active, the identity lock cannot be
        acquired within its bounded wait, any receipt, payload, toolchain or
        tree check fails, or the effective compiler does not match the
        receipt and manifest.
    """
    chosen = InstallHooks() if hooks is None else hooks
    with (
        operation_span(_LOGGER, "kani.snapshot.check") as span,
        _validated_under_lock(options) as (validated, deadline),
    ):
        # The probe must not run when the budget is already spent, and a probe
        # that spends the whole budget must not report a successful check.
        deadline.check("effective compiler verification")
        identity = deadline.supervise(
            "effective compiler verification",
            functools.partial(
                verify_effective_compiler,
                validated,
                chosen.probe,
                deadline.remaining,
            ),
        )
        deadline.check("effective compiler verification")
        span.finish("valid")
        return identity


@contextlib.contextmanager
def _validated_under_lock(
    options: InstallOptions,
) -> cabc.Generator[tuple[ValidatedGeneration, Deadline]]:
    """Yield the validated active generation while its lock stays held.

    Supervising these reads keeps a hung filesystem from holding the identity
    lock past the deadline, and a validation that spent the whole budget is
    refused rather than reported as successful.
    """
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
        # The filesystem validation must not outlive the budget, so it runs
        # under the same supervision as the reads.
        deadline.check("active generation validation")
        validated = deadline.supervise(
            "active generation validation",
            functools.partial(
                validate_generation,
                options,
                manifest,
                generation,
                checkpoint=deadline.checkpoint("active generation validation"),
            ),
        )
        # `run_within` returns as soon as its worker has finished, and that
        # can be after the budget expired, so a validation that spent the
        # whole budget must not report a successful check.
        deadline.check("active generation validation")
        yield validated, deadline
