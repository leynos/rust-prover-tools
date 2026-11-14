"""Concurrency contracts for snapshot installation.

The invariant is that active state names only a complete, validated
generation. These tests force the cases a sequential model cannot: overlapping
installs agree on one generation, an owner killed while holding the lock
releases it, and a bounded lock timeout refuses rather than hanging. One
witness stalls an owner inside its pipeline so the contender's refusal is
observed at the pipeline boundary rather than only around a bare lock; the
in-process two-actor model lives in `test_kani_snapshot_lock_model`.
Sequential activation safety is covered in `test_kani_snapshot_activation`.
"""

from __future__ import annotations

import dataclasses as dc
import multiprocessing
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    check_active_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.install_state import load_active
from tests.kani_snapshot_actors import (
    acquire_lock,
    hold_lock,
    hold_lock_until_killed,
    install_after_barrier,
    install_and_stall_inside_setup,
    repair_and_stall_before_publishing,
)
from tests.kani_snapshot_fixtures import make_pin
from tests.kani_snapshot_install_fixtures import (
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.timeout(30)
def test_second_process_waits_for_the_identity_lock(tmp_path: Path) -> None:
    """A live owner excludes a second installer until it releases."""
    context = multiprocessing.get_context("spawn")
    pin = make_pin()
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=hold_lock,
        args=(pin, str(tmp_path), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(10), "holder did not acquire the identity lock"
        outcome = context.Queue()
        contender = context.Process(
            target=acquire_lock,
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
        target=hold_lock,
        args=(pin, str(tmp_path), ready, release),
    )
    holder.start()
    assert ready.wait(10), "holder did not acquire the identity lock"
    release.set()
    holder.join(10)
    assert holder.exitcode == 0, "holder crashed"
    outcome = context.Queue()
    contender = context.Process(
        target=acquire_lock,
        args=(pin, str(tmp_path), outcome),
    )
    contender.start()
    contender.join(10)
    assert contender.exitcode == 0, "contender crashed"
    assert outcome.get(timeout=5) == "acquired", "lock was not free after exit"


@pytest.mark.timeout(120)
def test_overlapping_installs_publish_one_generation(tmp_path: Path) -> None:
    """Two installers starting together agree on a single active generation."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    barrier = context.Barrier(2)
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=install_after_barrier,
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
    check_active_snapshot(install_options(case), install_hooks(case))


@pytest.mark.timeout(120)
def test_killed_staging_owner_releases_the_lock(tmp_path: Path) -> None:
    """A killed owner cannot strand the identity lock for later installs."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    ready = context.Event()
    owner = context.Process(
        target=hold_lock_until_killed,
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
        check_active_snapshot(options, install_hooks(case))
    finally:
        owner.join(10)


@pytest.mark.timeout(120)
def test_contention_inside_the_pipeline_withholds_publication(
    tmp_path: Path,
) -> None:
    """A peer refused mid-pipeline cannot publish, and later reuses the result.

    The owner is stalled in setup, so it holds the identity lock with staging
    already complete and no active pointer yet. The contender is refused at
    that boundary rather than around a bare lock: it must not leave state of
    its own behind, and once the owner finishes the retry must reuse the
    owner's generation instead of publishing a second one.
    """
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    in_pipeline = context.Event()
    resume = context.Event()
    outcomes = context.Queue()
    generation = ""
    owner = context.Process(
        target=install_and_stall_inside_setup,
        args=(case, in_pipeline, resume, outcomes),
    )
    owner.start()
    try:
        assert in_pipeline.wait(20), "the owner never reached setup"
        layout = snapshot_layout(case.pin, cache_home=case.cache_home)
        assert load_active(layout) is None, "a stalled pipeline published a generation"
        contender = dc.replace(
            install_options(case),
            lock_timeout_seconds=0.2,
        )
        with pytest.raises(ProverToolError, match="timed out"):
            install_snapshot(contender, install_hooks(case))
        assert load_active(layout) is None, (
            "the refused contender activated a generation"
        )
        resume.set()
        generation = typ.cast("str", outcomes.get(timeout=60))
    finally:
        resume.set()
        owner.join(30)

    assert owner.exitcode == 0, "the stalled owner crashed"
    assert generation, "the stalled owner reported nothing"
    assert not generation.startswith("error:"), (
        f"the stalled owner failed: {generation}"
    )
    options = install_options(case)
    assert active_generation(case) == generation, "the owner did not publish"
    install_snapshot(options, install_hooks(case))
    assert active_generation(case) == generation, (
        "the retry published a second generation instead of reusing"
    )
    check_active_snapshot(options, install_hooks(case))


@pytest.mark.timeout(120)
def test_install_times_out_while_another_owner_holds_the_lock(tmp_path: Path) -> None:
    """A bounded wait refuses rather than hangs, and leaves no active state."""
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    ready = context.Event()
    release = context.Event()
    owner = context.Process(
        target=hold_lock,
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
    check_active_snapshot(install_options(case), install_hooks(case))


@pytest.mark.timeout(120)
def test_check_is_refused_while_a_repair_rewrites_the_cache(
    tmp_path: Path,
) -> None:
    """A reader must not validate a cache a live repair is rewriting.

    Damaging the cached backend forces the repair path while the active
    generation's tree stays intact. The repair is stalled after verifying its
    freshly staged payloads but before publishing any of them, so the shared
    downloads still hold the damaged copy. A reader with a short lock timeout
    must be refused at that boundary instead of reporting the damage, and must
    succeed once the repair completes.
    """
    context = multiprocessing.get_context("spawn")
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    backend = layout.downloads_dir / "backend.tar.gz"
    backend.write_bytes(backend.read_bytes() + b"damaged")
    in_publish = context.Event()
    resume = context.Event()
    outcomes = context.Queue()
    repairer = context.Process(
        target=repair_and_stall_before_publishing,
        args=(case, in_publish, resume, outcomes),
    )
    repairer.start()
    try:
        assert in_publish.wait(20), "the repair never reached publication"
        reader = dc.replace(install_options(case), lock_timeout_seconds=0.2)
        with pytest.raises(ProverToolError, match="timed out"):
            check_active_snapshot(reader, install_hooks(case))
        resume.set()
        report = typ.cast("str", outcomes.get(timeout=60))
    finally:
        resume.set()
        repairer.join(30)

    assert repairer.exitcode == 0, "the repair process crashed"
    assert report == "repaired", f"the repair failed: {report}"
    check_active_snapshot(install_options(case), install_hooks(case))
