"""Real two-process locking contracts for snapshot installation."""

from __future__ import annotations

import multiprocessing
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.install_state import identity_lock
from tests.kani_snapshot_fixtures import make_pin

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
