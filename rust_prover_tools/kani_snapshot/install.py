"""Bounded, failure-safe installation and activation of a snapshot generation.

The pipeline verifies the manifest digest, pin binding, attestation, checksums,
payload digests and archive structures before it invokes setup. Setup writes
only generation-private homes; the active pointer changes only after the
receipt is durable and the effective compiler has been observed.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import os
import shutil
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_extract import extract_archive
from rust_prover_tools.kani_snapshot.identity import (
    build_identity_report,
    runtime_for,
    snapshot_layout,
)
from rust_prover_tools.kani_snapshot.install_model import (
    InstallHooks,
    InstallOptions,
)
from rust_prover_tools.kani_snapshot.install_state import (
    active_manifest,
    ensure_private_directory,
    fsync_directory,
    identity_lock,
    load_active,
    validate_generation,
    write_active,
)
from rust_prover_tools.kani_snapshot.install_transport import (
    asset_by_role,
    bundle_policy,
    stage_assets,
)
from rust_prover_tools.kani_snapshot.receipt import build_receipt, render_receipt
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    SnapshotRuntime,
    new_generation_name,
)
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence

__all__ = (
    "InstallHooks",
    "InstallOptions",
    "check_snapshot",
    "install_snapshot",
)

_LOGGER = logging.getLogger(__name__)
_PRIVATE_FILE_MODE: typ.Final[int] = 0o600


@dc.dataclass(frozen=True, slots=True)
class _InstallContext:
    """The budgets and external operations for one installation."""

    options: InstallOptions
    deadline: _Deadline
    hooks: InstallHooks


@dc.dataclass(slots=True)
class _Deadline:
    """A monotonic installation budget."""

    expires_at: float

    def check(self, phase: str) -> None:
        """Fail when the budget has expired."""
        if time.monotonic() >= self.expires_at:
            msg = f"snapshot installation deadline expired during {phase}"
            raise ProverToolError(msg)

    def remaining(self) -> float:
        """Return the seconds left, never negative."""
        return max(0.0, self.expires_at - time.monotonic())


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
        Raised when verification fails or the overall deadline expires. The
        previously active generation and pointer are left untouched.
    """
    chosen = InstallHooks() if hooks is None else hooks
    if options.assets_dir is None:
        msg = (
            "snapshot installation requires --asset-dir until the producer "
            "release transport is available"
        )
        raise ProverToolError(msg)
    deadline = _Deadline(time.monotonic() + options.deadline_seconds)
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
            check_deadline=deadline.check,
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

    Raises
    ------
    ProverToolError
        Raised when no generation is active or any receipt, payload,
        toolchain or compiler check fails.
    """
    chosen = InstallHooks() if hooks is None else hooks
    layout = snapshot_layout(options.pin, cache_home=options.cache_home)
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
        pointer = load_active(layout)
        if pointer is None:
            return None
        manifest = active_manifest(options.pin, layout)
        generation = snapshot_layout(
            options.pin,
            cache_home=options.cache_home,
            generation=pointer.generation,
        )
        context.deadline.check("active generation validation")
        identity = validate_generation(
            options,
            manifest,
            generation,
            context.hooks.probe,
            context.deadline.remaining(),
        )
    except ProverToolError as exc:
        # The exception text can embed filesystem paths, so the event carries
        # only the bounded class name as its reason category.
        log_event(_LOGGER, "kani.install.repair", reason=type(exc).__name__)
        return None
    log_event(_LOGGER, "kani.install.reuse", generation=pointer.generation)
    return identity


def _install_generation(
    context: _InstallContext,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
) -> SnapshotIdentity:
    """Run setup in a new generation and publish it only after validation."""
    ensure_private_directory(layout.generation_dir)
    runtime = runtime_for(layout, manifest)
    extract_archive(
        layout.downloads_dir / asset_by_role(manifest, "launcher").name,
        layout.launcher_dir,
        policy=bundle_policy(manifest.backend.version),
    )
    context.deadline.check("setup")
    backend = layout.downloads_dir / asset_by_role(manifest, "backend").name
    # Setup and the probe run external processes, so each receives the
    # remaining budget rather than merely being checked before and after.
    context.hooks.setup(runtime, backend, context.deadline.remaining())
    context.deadline.check("setup")
    _install_launcher_binaries(runtime, layout)
    evidence = context.hooks.probe(runtime, context.deadline.remaining())
    _require_evidence(evidence, manifest, runtime)
    identity = build_identity_report(context.options.pin, manifest, runtime)
    receipt = build_receipt(context.options.pin, manifest, identity, evidence)
    _write_receipt(layout, render_receipt(receipt))
    context.deadline.check("activation")
    write_active(layout, layout.generation_dir.name)
    log_event(
        _LOGGER,
        "kani.install.activated",
        generation=layout.generation_dir.name,
    )
    return identity


def _install_launcher_binaries(
    runtime: SnapshotRuntime,
    layout: SnapshotLayout,
) -> None:
    """Place the packaged launcher beside the backend binaries."""
    source = layout.launcher_dir / "bin"
    target = runtime.kani_dir / "bin"
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        destination = target / entry.name
        if destination.exists():
            continue
        try:
            shutil.copy2(entry, destination)
        except OSError as exc:
            msg = f"failed to install snapshot launcher '{entry.name}': {exc}"
            raise ProverToolError(msg) from exc


def _require_evidence(
    evidence: SetupEvidence,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> None:
    """Require setup to have produced the reviewed compiler identity."""
    expected_link = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
    checks = (
        ("rustc", evidence.observed.rustc_vv, manifest.toolchain.rustc_vv),
        (
            "kani-compiler",
            evidence.observed.kani_compiler,
            manifest.toolchain.kani_compiler,
        ),
        ("toolchain link", evidence.toolchain_link_target, expected_link),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            msg = f"snapshot {name} identity does not match the manifest"
            raise ProverToolError(msg)


def _write_receipt(layout: SnapshotLayout, data: bytes) -> None:
    """Publish the receipt durably inside the generation."""
    pending = layout.receipt_path.with_name(
        f".receipt-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        with pending.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        pending.chmod(_PRIVATE_FILE_MODE)
        pending.replace(layout.receipt_path)
        fsync_directory(layout.generation_dir)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to write snapshot receipt: {exc}"
        raise ProverToolError(msg) from exc
