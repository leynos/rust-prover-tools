"""Interruption and seeded-fault contracts for snapshot installation.

Lock contention is covered in `test_kani_snapshot_concurrency`. These tests
interrupt the pipeline itself: a process killed while staging, and a fault
raised after setup has begun. In both cases a previously active generation must
survive, and a later install must recover without a partial pointer.
"""

from __future__ import annotations

import dataclasses as dc
import multiprocessing
import os
import time
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import install_copy, install_transport
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    check_active_snapshot,
    install_snapshot,
)
from tests.kani_snapshot_install_fixtures import (
    SnapshotCase,
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from multiprocessing import queues, synchronize
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout


def _install_and_stall_during_staging(
    case: SnapshotCase,
    marker: Path,
    partial_name: str,
    probe: cabc.Callable[[SnapshotRuntime], SetupEvidence] | None = None,
) -> None:
    """Install, stall after a partial staged write, then wait to be killed.

    The parent kills this process while it sleeps, so the partial pending file
    and the marker exist first. The partial file models a real interrupted
    transfer rather than a stall before any byte is written. A caller-supplied
    probe can force the repair path before staging begins.
    """
    real_copy = install_transport._copy_local

    def stall(
        source: Path,
        destination_dir: Path,
        file: install_copy.TransportFile,
        checkpoint: cabc.Callable[[], None] | None = None,
    ) -> Path:
        """Write half of one asset, announce staging, then block.

        Half the file is strictly shorter than the source even for the small
        fixture archives, so the partial write stays recognisable as
        incomplete rather than relying on a fixed chunk being shorter.
        """
        if file.name != partial_name:
            return real_copy(source, destination_dir, file, checkpoint)
        data = (source / file.name).read_bytes()
        pending = destination_dir / f".{file.name}.{os.getpid()}.pending"
        pending.write_bytes(data[: max(1, len(data) // 2)])
        marker.write_text("staging", encoding="utf-8")
        time.sleep(60)
        return real_copy(source, destination_dir, file, checkpoint)

    # `stage_assets` looks the helper up in the module namespace at call time,
    # which is what makes this substitution visible without editing production
    # code. Assignment through `vars` keeps the test seam's looser signature
    # out of the module's checked surface.
    vars(install_transport)["_copy_local"] = stall
    install_snapshot(install_options(case), install_hooks(case, probe=probe))


def _repair_and_stall_during_staging(
    case: SnapshotCase,
    marker: Path,
    partial_name: str,
) -> None:
    """Force a repair through a transient probe failure, then stall mid-staging.

    Nothing about the active generation is damaged: its receipt, tree, Python
    record and cached payloads stay valid, and only the one probe observation
    fails. An interrupted repair therefore must not be able to blame the
    generation it was about to replace.
    """

    def transient_probe(_runtime: SnapshotRuntime) -> SetupEvidence:
        """Fail one observation, as a briefly unavailable compiler would."""
        msg = "transient probe failure"
        raise ProverToolError(msg)

    _install_and_stall_during_staging(case, marker, partial_name, transient_probe)


def _install_with_seeded_fault(
    case: SnapshotCase,
    in_setup: synchronize.Event,
    peer_attempting: synchronize.Event,
    outcomes: queues.Queue[str],
) -> None:
    """Fail inside setup once the peer has started its own install attempt."""

    def faulting_setup(
        _runtime: object,
        _bundle: Path,
        _timeout_seconds: float | None,
    ) -> None:
        """Report entry, wait for the peer, then fail the pipeline."""
        in_setup.set()
        if not peer_attempting.wait(20):
            msg = "peer never attempted its install"
            raise ProverToolError(msg)
        msg = "seeded setup fault"
        raise ProverToolError(msg)

    hooks = dc.replace(install_hooks(case), setup=faulting_setup)
    try:
        install_snapshot(install_options(case), hooks)
    except ProverToolError:
        outcomes.put("failed")
    else:
        outcomes.put("unexpected-success")


def _install_and_report(
    case: SnapshotCase,
    attempting: synchronize.Event,
    outcomes: queues.Queue[str],
) -> None:
    """Announce the attempt, then install and report the active generation."""
    try:
        attempting.set()
        install_snapshot(install_options(case), install_hooks(case))
        outcomes.put(active_generation(case))
    except Exception as exc:  # ruff: ignore[blind-except] - the parent reports it
        outcomes.put(f"error:{type(exc).__name__}:{exc}")


@pytest.mark.timeout(120)
def test_killed_installer_after_a_partial_write_leaves_recovery_intact(
    tmp_path: Path,
) -> None:
    """A kill after a partial staged write neither publishes nor blocks repair.

    Damaged downloads force the repair path, so the child is re-staging the
    backend from the intact source release while an older generation is
    active. The child is killed after writing part of the new backend into its
    private staging directory. The pointer must still name the old generation,
    and the next install must repair the cache from the source release on its
    own: the test never restores the damaged cache, so recovery cannot be
    confused with manual cleanup.
    """
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    generation = active_generation(case)
    # Damaging the cached copy, not the source release, makes the active
    # generation fail revalidation while a fresh staging can still succeed.
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    backend = layout.downloads_dir / "backend.tar.gz"
    backend.write_bytes(backend.read_bytes() + b"damaged")

    marker = tmp_path / "staging-started"
    owner = context.Process(
        target=_install_and_stall_during_staging,
        args=(case, marker, backend.name),
    )
    owner.start()
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the installer never reached staging"
        owner.kill()
        owner.join(10)
        assert owner.exitcode is not None, "the killed installer did not exit"
    finally:
        owner.join(10)

    assert active_generation(case) == generation, (
        "a killed staging owner changed the active generation"
    )
    partials = _partial_staging_files(layout)
    assert partials, "the killed installer left no partial staged write"
    assert all(path.stat().st_size < backend.stat().st_size for path in partials), (
        "the partial staged write was actually complete"
    )

    # No restoration happens here: the intact source release is the only
    # repair input, exactly as a real operator would leave it.
    install_snapshot(install_options(case), install_hooks(case))
    check_active_snapshot(install_options(case), install_hooks(case))
    previous = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=generation,
    )
    assert previous.receipt_path.is_file(), (
        "recovery destroyed the previously active generation"
    )
    assert previous.python_root_record_path.is_file(), (
        "recovery destroyed the previously active generation's Python record"
    )


@pytest.mark.timeout(120)
def test_interrupted_repair_leaves_the_previous_generation_usable(
    tmp_path: Path,
) -> None:
    """A killed repair must not invalidate the generation it was replacing.

    A transient probe failure forces the repair path while every artefact of
    the active generation is still intact. The repair is killed after a partial
    staged write, so the pointer must still name the old generation and a
    normal check, with a healthy probe, must validate it before any recovery
    runs. A repair that damaged shared state before publishing would fail here.
    """
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    generation = active_generation(case)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    backend = layout.downloads_dir / "backend.tar.gz"
    marker = tmp_path / "repair-staging-started"
    owner = context.Process(
        target=_repair_and_stall_during_staging,
        args=(case, marker, backend.name),
    )
    owner.start()
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the repair never reached staging"
        owner.kill()
        owner.join(10)
        assert owner.exitcode is not None, "the killed repair did not exit"
    finally:
        owner.join(10)

    assert active_generation(case) == generation, (
        "a killed repair changed the active generation"
    )
    partials = _partial_staging_files(layout)
    assert partials, "the killed repair left no partial staged write"
    assert all(path.stat().st_size < backend.stat().st_size for path in partials), (
        "the partial staged write was actually complete"
    )

    # A healthy probe validates the old generation with no recovery in between:
    # the interrupted repair did not touch the evidence it would have replaced.
    check_active_snapshot(install_options(case), install_hooks(case))
    assert active_generation(case) == generation, (
        "validating the previous generation changed the active pointer"
    )

    install_snapshot(install_options(case), install_hooks(case))
    check_active_snapshot(install_options(case), install_hooks(case))
    previous = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=generation,
    )
    assert previous.receipt_path.is_file(), (
        "recovery destroyed the previously active generation"
    )
    assert previous.python_root_record_path.is_file(), (
        "recovery destroyed the previously active generation's Python record"
    )


def _partial_staging_files(layout: SnapshotLayout) -> list[Path]:
    """Return pending files left in abandoned private staging directories."""
    return [
        pending
        for staging in layout.identity_dir.glob(".staging-*")
        if staging.is_dir()
        for pending in staging.glob(".*.pending")
    ]


@pytest.mark.timeout(120)
def test_seeded_setup_fault_leaves_one_valid_generation(tmp_path: Path) -> None:
    """One actor's in-pipeline fault cannot strand a peer's install.

    The fault is seeded at setup, after staging succeeded and while the actor
    holds the identity lock. The peer announces its attempt before the fault
    fires, so the run proves that a failure inside the pipeline leaves the
    pointer unwritten and a waiting installer able to publish the only valid
    generation.
    """
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    in_setup = context.Event()
    peer_attempting = context.Event()
    outcomes = context.Queue()
    faulty = context.Process(
        target=_install_with_seeded_fault,
        args=(case, in_setup, peer_attempting, outcomes),
    )
    peer = context.Process(
        target=_install_and_report,
        args=(case, peer_attempting, outcomes),
    )
    faulty.start()
    peer_started = False
    try:
        assert in_setup.wait(20), "the faulty installer never reached setup"
        peer.start()
        peer_started = True
        reports = [typ.cast("str", outcomes.get(timeout=60)) for _ in range(2)]
    finally:
        if peer_started:
            peer.join(30)
        faulty.join(30)

    assert faulty.exitcode == 0, "the faulty installer crashed"
    assert peer.exitcode == 0, "the waiting installer crashed"
    assert "failed" in reports, f"the seeded fault did not fire: {reports}"
    assert "unexpected-success" not in reports, (
        f"the faulty installer reported success: {reports}"
    )
    generation = next(report for report in reports if report != "failed")
    assert not generation.startswith("error:"), f"the peer install failed: {reports}"
    assert active_generation(case) == generation, (
        "the peer's generation is not the active one"
    )
    check_active_snapshot(install_options(case), install_hooks(case))
