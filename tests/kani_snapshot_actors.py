"""Process entry points for the snapshot concurrency contracts.

The concurrency tests coordinate several operating-system processes, so every
spawn target must be a module-level function importable by name rather than a
closure. Keeping them here leaves the test module free to state the contracts
it checks, and groups the deliberate stalls — inside setup, inside publication,
and around the identity lock — in one place.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import install_transport, publication
from rust_prover_tools.kani_snapshot.install import install_snapshot
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.install_state import identity_lock
from tests.kani_snapshot_install_fixtures import (
    SnapshotCase,
    active_generation,
    install_hooks,
    install_options,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from multiprocessing import queues, synchronize

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "acquire_lock",
    "hold_lock",
    "hold_lock_until_killed",
    "install_after_barrier",
    "install_and_stall_inside_setup",
    "repair_and_stall_before_publishing",
)


def hold_lock(
    pin: SnapshotPin,
    cache_home: str,
    ready: synchronize.Event,
    release: synchronize.Event,
) -> None:
    """Hold the identity lock until the parent permits release."""
    options = InstallOptions(pin=pin, cache_home=Path(cache_home))
    with identity_lock(options, timeout=10):
        ready.set()
        release.wait(10)


def hold_lock_until_killed(
    pin: SnapshotPin,
    cache_home: str,
    ready: synchronize.Event,
) -> None:
    """Hold the identity lock until the parent kills this process.

    A plain sleep avoids the shared event's condition variables, whose
    internal sleeper accounting a killed waiter would leave inconsistent.
    """
    options = InstallOptions(pin=pin, cache_home=Path(cache_home))
    with identity_lock(options, timeout=10):
        ready.set()
        time.sleep(60)


def acquire_lock(
    pin: SnapshotPin,
    cache_home: str,
    outcome: queues.Queue[str],
) -> None:
    """Try to acquire the lock and report whether it was refused."""
    options = InstallOptions(pin=pin, cache_home=Path(cache_home))
    try:
        with identity_lock(options, timeout=0.2):
            outcome.put("acquired")
    except ProverToolError as exc:
        outcome.put(f"refused: {exc}")


def install_after_barrier(
    case: SnapshotCase,
    barrier: synchronize.Barrier,
    outcomes: queues.Queue[str],
) -> None:
    """Install once its peer has started and report the observed generation."""
    try:
        barrier.wait(timeout=10)
        install_snapshot(install_options(case), install_hooks(case))
        outcomes.put(active_generation(case))
    except Exception as exc:  # ruff: ignore[blind-except] - the parent reports it
        outcomes.put(f"error:{type(exc).__name__}:{exc}")


def install_and_stall_inside_setup(
    case: SnapshotCase,
    in_pipeline: synchronize.Event,
    resume: synchronize.Event,
    outcomes: queues.Queue[str],
) -> None:
    """Stall inside setup, after staging, while holding the identity lock."""
    hooks = install_hooks(case)

    def stalling_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Announce that the pipeline is mid-flight, then wait to continue."""
        in_pipeline.set()
        if not resume.wait(30):
            msg = "parent never released the stalled pipeline"
            raise ProverToolError(msg)
        hooks.setup(runtime, bundle, timeout_seconds)

    try:
        install_snapshot(
            install_options(case),
            dc.replace(hooks, setup=stalling_setup),
        )
        outcomes.put(active_generation(case))
    except Exception as exc:  # ruff: ignore[blind-except] - the parent reports it
        outcomes.put(f"error:{type(exc).__name__}:{exc}")


def repair_and_stall_before_publishing(
    case: SnapshotCase,
    in_publish: synchronize.Event,
    resume: synchronize.Event,
    outcomes: queues.Queue[str],
) -> None:
    """Repair a damaged cache, stalling between verification and publication.

    The stall sits after every staged payload has been verified and before any
    of them is published, so the shared downloads still hold the damaged
    content while the identity lock is held.
    """
    real_publish = publication.publish_staged

    def stalling_publish(
        staged: publication.Publication,
        *,
        budget: InstallBudget | None = None,
        checkpoint: cabc.Callable[[], None] | None = None,
    ) -> None:
        """Announce the mid-repair state, then publish when permitted."""
        in_publish.set()
        if not resume.wait(30):
            msg = "parent never released the stalled repair"
            raise ProverToolError(msg)
        real_publish(staged, budget=budget, checkpoint=checkpoint)

    # `stage_assets` resolves the helper from the module namespace at call
    # time, so this substitution needs no seam in production code.
    vars(install_transport)["publish_staged"] = stalling_publish
    try:
        install_snapshot(install_options(case), install_hooks(case))
        outcomes.put("repaired")
    except Exception as exc:  # ruff: ignore[blind-except] - the parent reports it
        outcomes.put(f"error:{type(exc).__name__}:{exc}")
