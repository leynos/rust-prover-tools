"""Bounded, failure-safe installation and activation of a snapshot generation.

The pipeline verifies the manifest digest, pin binding, attestation, checksums,
payload digests and archive structures before it invokes setup. Setup writes
only generation-private homes; the active pointer changes only after the
receipt is durable and the effective compiler has been observed.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import logging
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.activation import write_active
from rust_prover_tools.kani_snapshot.archive_extract import extract_archive
from rust_prover_tools.kani_snapshot.durability import (
    confirm_activation,
    flush_directory,
)
from rust_prover_tools.kani_snapshot.identity import (
    build_identity_report,
    runtime_for,
    snapshot_layout,
)
from rust_prover_tools.kani_snapshot.install_cache import validate_installed_tree
from rust_prover_tools.kani_snapshot.install_generation import (
    flush_generation_entries,
    install_launcher_binaries,
    require_evidence,
    write_generation_file,
)
from rust_prover_tools.kani_snapshot.install_model import (
    Deadline,
    InstallHooks,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.install_state import (
    active_manifest,
    ensure_private_directory,
    identity_lock,
    load_active,
    publication_pending,
    require_runtime_files,
    validate_generation,
)
from rust_prover_tools.kani_snapshot.install_transport import (
    asset_by_role,
    bundle_policy,
    stage_assets,
)
from rust_prover_tools.kani_snapshot.python_root import record_python_root
from rust_prover_tools.kani_snapshot.receipt import build_receipt, render_receipt
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    new_generation_name,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import verify_toolchain
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest

__all__ = (
    "InstallHooks",
    "InstallOptions",
    "check_snapshot",
    "install_snapshot",
)

_LOGGER = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class _InstallContext:
    """The budgets and external operations for one installation."""

    options: InstallOptions
    deadline: Deadline
    hooks: InstallHooks


def install_snapshot(
    options: InstallOptions,
    hooks: InstallHooks | None = None,
) -> SnapshotIdentity:
    """Install or reuse one exact snapshot generation.

    Parameters
    ----------
    options : InstallOptions
        Reviewed pin, local asset transport and budgets.
    hooks : InstallHooks | None
        External setup, probe and attestation operations. Defaults to the
        packaged launcher, the live compiler and `gh`.

    Returns
    -------
    SnapshotIdentity
        The active, fully validated generation.

    Raises
    ------
    ProverToolError
        Raised when verification fails or the overall deadline expires.
        Failures before publication leave the previously active generation and
        pointer untouched. A durability failure after the rename reports the
        generation that is already active rather than claiming that nothing
        was published.
    """
    chosen = InstallHooks() if hooks is None else hooks
    if options.assets_dir is None:
        msg = (
            "snapshot installation requires --asset-dir until the producer "
            "release transport is available"
        )
        raise ProverToolError(msg)
    deadline = Deadline(time.monotonic() + options.deadline_seconds)
    deadline.check("installation start")
    layout = snapshot_layout(options.pin, cache_home=options.cache_home)
    ensure_private_directory(layout.identity_dir)
    context = _InstallContext(options=options, deadline=deadline, hooks=chosen)
    with identity_lock(
        options,
        timeout=min(options.lock_timeout_seconds, options.deadline_seconds),
    ):
        reusable = _reuse_active(context)
        if reusable is not None:
            return reusable
        deadline.check("asset staging")
        manifest = stage_assets(
            options,
            layout,
            chosen,
            budget=deadline,
        )
        generation = snapshot_layout(
            options.pin,
            cache_home=options.cache_home,
            generation=new_generation_name(),
        )
        return _install_generation(context, manifest, generation)


def check_snapshot(
    options: InstallOptions,
    hooks: InstallHooks | None = None,
) -> SnapshotIdentity:
    """Validate and report the active generation without installing.

    The pointer is resolved and the generation validated while the identity
    lock is held. A repair publishes its cached payloads file by file under
    that same lock, so reading without it could validate a half-repaired cache
    and report a failure that the running repair is about to resolve.

    Raises
    ------
    ProverToolError
        Raised when no generation is active, the identity lock cannot be
        acquired within its bounded wait, or any receipt, payload, toolchain
        or compiler check fails.
    """
    chosen = InstallHooks() if hooks is None else hooks
    with identity_lock(
        options,
        timeout=min(options.lock_timeout_seconds, options.deadline_seconds),
    ):
        layout = snapshot_layout(options.pin, cache_home=options.cache_home)
        if publication_pending(layout):
            msg = "snapshot cache publication did not finish; run install to repair"
            raise ProverToolError(msg)
        pointer = load_active(layout)
        if pointer is None:
            msg = "snapshot is not installed; run install first"
            raise ProverToolError(msg)
        manifest = active_manifest(options.pin, layout)
        generation = snapshot_layout(
            options.pin,
            cache_home=options.cache_home,
            generation=pointer.generation,
        )
        return validate_generation(options, manifest, generation, chosen.probe)


def _reuse_active(context: _InstallContext) -> SnapshotIdentity | None:
    """Return a valid active generation, or `None` when repair is needed."""
    options = context.options
    layout = snapshot_layout(options.pin, cache_home=options.cache_home)
    try:
        # Both reads touch the shared cache, so a hung filesystem must not hold
        # the identity lock past the budget. Abandoning a read is safe because
        # it mutates nothing.
        if context.deadline.supervise(
            "publication state read",
            functools.partial(publication_pending, layout),
        ):
            # A cache carrying the marker may be half-repaired, so reuse would
            # validate a state that a later publication is still changing.
            log_event(_LOGGER, "kani.install.repair", reason="PublicationPending")
            return None
        pointer = context.deadline.supervise(
            "active pointer read",
            functools.partial(load_active, layout),
        )
        if pointer is None:
            return None
        manifest = context.deadline.supervise(
            "manifest read",
            functools.partial(active_manifest, options.pin, layout),
        )
        generation = snapshot_layout(
            options.pin,
            cache_home=options.cache_home,
            generation=pointer.generation,
        )
        context.deadline.check("active generation validation")
        identity = context.deadline.supervise(
            "active generation validation",
            functools.partial(
                validate_generation,
                options,
                manifest,
                generation,
                context.hooks.probe,
                context.deadline.remaining,
                checkpoint=context.deadline.checkpoint("active generation validation"),
            ),
        )
    except ProverToolError as exc:
        if context.deadline.remaining() <= 0:
            # An exhausted budget is not evidence of a corrupt generation, and
            # repair could not finish, so report the deadline instead.
            raise
        # The exception text can embed filesystem paths, so the event carries
        # only the bounded class name as its reason category.
        log_event(_LOGGER, "kani.install.repair", reason=type(exc).__name__)
        return None
    # The probe was bounded by the budget left when it started, so a probe
    # that spent all of it must not report a successful reuse.
    _confirm_durable(layout, context.deadline)
    context.deadline.check("active generation reuse")
    log_event(_LOGGER, "kani.install.reuse", generation=pointer.generation)
    return identity


def _confirm_durable(layout: SnapshotLayout, deadline: Deadline) -> None:
    """Flush the activation directories before reporting an active generation.

    A previous activation may have published its record and then failed or
    timed out while flushing, leaving a visible but not durable generation.
    Reuse reissues those flushes, so a retry cannot report success until the
    activation is confirmed durable.
    """
    confirm_activation(layout, deadline)


def _install_generation(
    context: _InstallContext,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
) -> SnapshotIdentity:
    """Run setup in a new generation and publish it only after validation."""
    ensure_private_directory(layout.generation_dir)
    # The generation's own directory entry lives in `generations`, so that
    # parent must be durable before the pointer can be understood to name it.
    flush_directory(
        layout.generation_dir.parent,
        budget=context.deadline,
        phase="generation directory flush",
    )
    runtime = runtime_for(layout, manifest)
    context.deadline.supervise(
        "launcher extraction",
        functools.partial(
            extract_archive,
            layout.downloads_dir / asset_by_role(manifest, "launcher").name,
            layout.launcher_dir,
            policy=bundle_policy(manifest.backend.version),
            checkpoint=context.deadline.checkpoint("launcher extraction"),
        ),
    )
    context.deadline.check("setup")
    backend = layout.downloads_dir / asset_by_role(manifest, "backend").name
    # Setup and the probe run external processes, so each receives the
    # remaining budget rather than merely being checked before and after.
    context.hooks.setup(runtime, backend, context.deadline.remaining())
    context.deadline.check("setup")
    install_launcher_binaries(
        runtime,
        layout,
        budget=context.deadline,
    )
    # Setup owns `pyroot`, so its content is recorded before validation turns
    # the inventory's open-ended allowance for it into a closed contract.
    write_generation_file(
        layout.python_root_record_path,
        context.deadline.supervise(
            "Python root recording",
            functools.partial(
                record_python_root,
                runtime,
                checkpoint=context.deadline.checkpoint("Python root recording"),
            ),
        ),
        "Python root record",
        budget=context.deadline,
    )
    context.deadline.supervise(
        "installed tree validation",
        functools.partial(
            validate_installed_tree,
            layout,
            manifest,
            runtime,
            checkpoint=context.deadline.checkpoint("installed tree validation"),
        ),
    )
    # Activation must not publish executables the next check would reject, so
    # the same runtime-file contract `validate_generation` enforces is applied
    # here too, before the probe executes them.
    require_runtime_files(runtime)
    # The probe executes these bytes, so their digests are checked first.
    context.deadline.supervise(
        "toolchain verification",
        functools.partial(
            verify_toolchain,
            runtime,
            manifest,
            checkpoint=context.deadline.checkpoint("toolchain verification"),
        ),
    )
    evidence = context.hooks.probe(runtime, context.deadline.remaining())
    require_evidence(evidence, manifest, runtime)
    identity = build_identity_report(context.options.pin, manifest, runtime)
    receipt = build_receipt(context.options.pin, manifest, identity, evidence)
    write_generation_file(
        layout.receipt_path,
        render_receipt(receipt),
        "receipt",
        budget=context.deadline,
    )
    context.deadline.check("activation")
    # Every required entry inside the generation is now durable, so the
    # activation record cannot survive a crash while naming a partially
    # flushed generation.
    flush_generation_entries(
        runtime,
        layout,
        manifest,
        budget=context.deadline,
    )
    write_active(layout, layout.generation_dir.name, budget=context.deadline)
    log_event(
        _LOGGER,
        "kani.install.activated",
        generation=layout.generation_dir.name,
    )
    return identity
