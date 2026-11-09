"""Two-process concurrency contracts for snapshot installation.

The invariant is that active state names only a complete, validated
generation. These tests force the cases a sequential model cannot: overlapping
installs agree on one generation, an owner killed while holding the lock
releases it, and a bounded lock timeout refuses rather than hanging.
Sequential activation safety is covered in `test_kani_snapshot_activation`.
"""

from __future__ import annotations

import multiprocessing
import time
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import check_snapshot, install_snapshot
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.install_state import identity_lock, load_active
from tests.kani_snapshot_fixtures import make_pin
from tests.kani_snapshot_install_fixtures import (
    SnapshotCase,
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    from multiprocessing import queues, synchronize

    from rust_prover_tools.kani_snapshot.models import SnapshotPin


def _hold_lock(
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


def _hold_lock_until_killed(
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


def _acquire_lock(
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


@pytest.mark.timeout(30)
def test_second_process_waits_for_the_identity_lock(tmp_path: Path) -> None:
    """A live owner excludes a second installer until it releases."""
    context = multiprocessing.get_context("spawn")
    pin = make_pin()
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_lock,
        args=(pin, str(tmp_path), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(10), "holder did not acquire the identity lock"
        outcome = context.Queue()
        contender = context.Process(
            target=_acquire_lock,
            args=(pin, str(tmp_path), outcome),
        )
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0, "contender crashed"
        message = typ.cast("str", outcome.get(timeout=5))
        assert message.startswith("refused"), "contender was not refused the lock"
    finally:
        release.set()
        holder.join(10)
    assert holder.exitcode == 0, "holder crashed"


@pytest.mark.timeout(30)
def test_lock_is_available_after_the_owner_exits(tmp_path: Path) -> None:
    """The advisory lock is released by process exit, not by polling state."""
    context = multiprocessing.get_context("spawn")
    pin = make_pin()
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_lock,
        args=(pin, str(tmp_path), ready, release),
    )
    holder.start()
    assert ready.wait(10), "holder did not acquire the identity lock"
    release.set()
    holder.join(10)
    assert holder.exitcode == 0, "holder crashed"
    outcome = context.Queue()
    contender = context.Process(
        target=_acquire_lock,
        args=(pin, str(tmp_path), outcome),
    )
    contender.start()
    contender.join(10)
    assert contender.exitcode == 0, "contender crashed"
    assert outcome.get(timeout=5) == "acquired", "lock was not free after exit"


def _install_after_barrier(
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


@pytest.mark.timeout(120)
def test_overlapping_installs_publish_one_generation(tmp_path: Path) -> None:
    """Two installers starting together agree on a single active generation."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    barrier = context.Barrier(2)
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_install_after_barrier,
            args=(case, barrier, outcomes),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        reports = [typ.cast("str", outcomes.get(timeout=60)) for _ in workers]
    finally:
        for worker in workers:
            worker.join(30)

    assert all(worker.exitcode == 0 for worker in workers), "an installer crashed"
    failures = [report for report in reports if report.startswith("error:")]
    assert not failures, f"overlapping installs failed: {failures}"
    assert len(set(reports)) == 1, (
        f"installs published different generations: {reports}"
    )
    assert active_generation(case) == reports[0], "reported generation is not active"
    check_snapshot(install_options(case), install_hooks(case))


@pytest.mark.timeout(120)
def test_killed_staging_owner_releases_the_lock(tmp_path: Path) -> None:
    """A killed owner cannot strand the identity lock for later installs."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    ready = context.Event()
    owner = context.Process(
        target=_hold_lock_until_killed,
        args=(case.pin, str(case.cache_home), ready),
    )
    owner.start()
    try:
        assert ready.wait(10), "owner did not acquire the identity lock"
        owner.kill()
        owner.join(10)
        assert owner.exitcode is not None, "the killed owner did not exit"
        options = InstallOptions(
            pin=case.pin,
            assets_dir=case.assets_dir,
            cache_home=case.cache_home,
            lock_timeout_seconds=10,
        )
        install_snapshot(options, install_hooks(case))
        check_snapshot(options, install_hooks(case))
    finally:
        owner.join(10)


@pytest.mark.timeout(120)
def test_install_times_out_while_another_owner_holds_the_lock(tmp_path: Path) -> None:
    """A bounded wait refuses rather than hangs, and leaves no active state."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    ready = context.Event()
    release = context.Event()
    owner = context.Process(
        target=_hold_lock,
        args=(case.pin, str(case.cache_home), ready, release),
    )
    owner.start()
    try:
        assert ready.wait(10), "owner did not acquire the identity lock"
        options = InstallOptions(
            pin=case.pin,
            assets_dir=case.assets_dir,
            cache_home=case.cache_home,
            lock_timeout_seconds=0.2,
        )
        with pytest.raises(ProverToolError, match="timed out"):
            install_snapshot(options, install_hooks(case))
        assert (
            load_active(snapshot_layout(case.pin, cache_home=case.cache_home)) is None
        ), "a refused install activated a generation"
    finally:
        release.set()
        owner.join(10)
    assert owner.exitcode == 0, "owner crashed"

    install_snapshot(install_options(case), install_hooks(case))
    check_snapshot(install_options(case), install_hooks(case))
