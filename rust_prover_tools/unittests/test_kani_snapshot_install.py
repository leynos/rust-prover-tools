"""Contract tests for snapshot installation, activation and cache reuse."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import fcntl
import io
import json
import logging
import os
import pathlib
import shutil
import stat
import tarfile
import threading
import time
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import (
    activation,
    cleanup,
    completion,
    durability,
    install_copy,
    install_generation,
    install_state,
    install_transport,
    publication,
)
from rust_prover_tools.kani_snapshot import (
    install as install_module,
)
from rust_prover_tools.kani_snapshot.identity import runtime_for, snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    InstallOptions,
    check_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.install_model import Deadline
from rust_prover_tools.kani_snapshot.install_state import (
    load_active,
    validate_generation,
)
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION, FileRecord
from rust_prover_tools.kani_snapshot.receipt import ObservedToolchain, SetupEvidence
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    PUBLICATION_MARKER_PREFIX,
    SnapshotLayout,
    SnapshotRuntime,
    new_generation_name,
)
from tests.kani_snapshot_bundles import (
    CHECKSUMS_NAME,
    LIBRARY_PATH,
    MANIFEST_NAME,
    RUSTC_BYTES,
    payloads,
    publish_manifest,
    sha256_hex,
    tar_bytes,
    write_file,
)
from tests.kani_snapshot_install_fixtures import (
    PYTHON_ROOT_FILE,
    SnapshotCase,
    active_generation,
    install_hooks,
    install_options,
    make_case,
    rewrite_manifest_asset,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity
    from rust_prover_tools.kani_snapshot.install_model import (
        InstallBudget,
        ProbeRunner,
    )
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

_T = typ.TypeVar("_T")


@dc.dataclass(frozen=True, slots=True)
class _ExpiringBudget:
    """A budget that expires at exactly one named phase.

    Activation is tested at its publication boundary rather than by waiting for
    a real clock, so the refusal is deterministic and names the phase that was
    interrupted.
    """

    phase: str

    def check(self, phase: str) -> None:
        """Expire when the guarded publication gate is reached."""
        if phase == self.phase:
            msg = f"snapshot installation deadline expired during {phase}"
            raise ProverToolError(msg)

    def remaining(self) -> float:
        """Report a budget that is otherwise unspent."""
        return 60.0

    def supervise(self, phase: str, operation: cabc.Callable[[], _T]) -> _T:
        """Run local work as an unexpired budget would, without a thread."""
        del phase
        return operation()


@dc.dataclass(frozen=True, slots=True)
class _ExpiringSupervision:
    """A budget whose supervised phase times out instead of running.

    Durability failures happen inside a supervised operation rather than at a
    cooperative checkpoint, so this budget reproduces the expiry the worker
    wait would report without needing a real stalled `fsync`.
    """

    phase: str

    def check(self, phase: str) -> None:
        """Accept every cooperative checkpoint."""
        del phase

    def remaining(self) -> float:
        """Report a budget that has not expired."""
        return 60.0

    def supervise(self, phase: str, operation: cabc.Callable[[], _T]) -> _T:
        """Expire the named supervised phase and run every other one."""
        if phase == self.phase:
            msg = f"snapshot installation deadline expired during {phase}"
            raise ProverToolError(msg)
        return operation()


def _same_version_case(directory: Path, *, cache_home: Path) -> SnapshotCase:
    """Return a second snapshot that reports the first one's versions.

    Only the full commit identity and the published manifest bytes differ, so
    the two pins agree on every version string a consumer could compare.
    """
    case = make_case(directory)
    manifest = dc.replace(
        case.manifest,
        tag="kani-snapshot-2026-11-dddddddddddd-r1",
        upstream_commit="d" * 40,
        backend=dc.replace(case.manifest.backend, revision="d" * 40),
    )
    base_pin = dc.replace(
        case.pin,
        tag=manifest.tag,
        upstream_commit=manifest.upstream_commit,
    )
    pin = publish_manifest(case.assets_dir, manifest, base_pin=base_pin)
    return SnapshotCase(
        pin=pin,
        manifest=manifest,
        assets_dir=case.assets_dir,
        cache_home=cache_home,
    )


def test_same_reported_version_installs_into_disjoint_generations(
    tmp_path: Path,
) -> None:
    """Two snapshots reporting one upstream version never share a tree."""
    first = make_case(tmp_path / "first")
    second = _same_version_case(tmp_path / "second", cache_home=first.cache_home)
    assert first.pin.upstream_commit != second.pin.upstream_commit, (
        "the two pins must differ by their full commit identity"
    )
    assert first.manifest.toolchain.identifier == second.manifest.toolchain.identifier
    assert first.manifest.backend.version == second.manifest.backend.version
    assert (
        first.manifest.launcher_package_version
        == second.manifest.launcher_package_version
    )

    first_identity = install_snapshot(install_options(first), install_hooks(first))
    second_identity = install_snapshot(install_options(second), install_hooks(second))

    assert first_identity.backend_version == second_identity.backend_version
    assert first_identity.generation.parent != second_identity.generation.parent, (
        "identical versions shared one identity directory"
    )
    assert first_identity.rustc != second_identity.rustc, (
        "identical versions shared one toolchain"
    )
    assert active_generation(first) == first_identity.generation.name
    assert active_generation(second) == second_identity.generation.name
    # Both installations stay independently valid in the shared cache root.
    assert (
        check_snapshot(install_options(first), install_hooks(first)) == first_identity
    )
    assert (
        check_snapshot(install_options(second), install_hooks(second))
        == second_identity
    )


def test_install_activates_a_validated_generation(tmp_path: Path) -> None:
    """A complete local install activates a validated generation."""
    case = make_case(tmp_path)
    calls: list[Path] = []
    identity = install_snapshot(
        install_options(case),
        install_hooks(case, setup_calls=calls),
    )
    assert identity.generation.name == active_generation(case), (
        "active pointer does not name the installed generation"
    )
    assert identity.launcher.exists(), "installed launcher is missing"
    assert identity.generation.joinpath("receipt.json").is_file(), (
        "installed generation has no receipt"
    )
    assert [path.name for path in calls] == ["backend.tar.gz"], (
        "setup did not receive exactly the backend bundle"
    )


def test_second_install_reuses_the_active_generation(tmp_path: Path) -> None:
    """A repeated install returns the active generation without setup."""
    case = make_case(tmp_path)
    calls: list[Path] = []
    hooks = install_hooks(case, setup_calls=calls)
    first = install_snapshot(install_options(case), hooks)
    second = install_snapshot(install_options(case), hooks)
    assert first == second, "repeated install returned a different identity"
    assert len(calls) == 1, "repeated install ran setup again"


def test_install_rejects_a_missing_asset(tmp_path: Path) -> None:
    """A missing local payload is reported before setup runs."""
    case = make_case(tmp_path)
    (case.assets_dir / "backend.tar.gz").unlink()
    calls: list[Path] = []
    with pytest.raises(ProverToolError, match=r"backend\.tar\.gz"):
        install_snapshot(
            install_options(case),
            install_hooks(case, setup_calls=calls),
        )
    assert calls == [], "setup ran despite a missing asset"


def test_install_rejects_a_manifest_outside_the_reviewed_pin(tmp_path: Path) -> None:
    """A manifest whose bytes differ from the pin is refused."""
    case = make_case(tmp_path)
    changed = dc.replace(case.pin, manifest_sha256="0" * 64)
    options = InstallOptions(
        pin=changed,
        assets_dir=case.assets_dir,
        cache_home=tmp_path,
    )
    with pytest.raises(ProverToolError, match=r"manifest digest"):
        install_snapshot(options, install_hooks(case))


def test_install_rejects_a_failed_attestation(tmp_path: Path) -> None:
    """A rejected attestation stops installation before setup."""
    case = make_case(tmp_path)
    calls: list[Path] = []

    def reject(_policy: object, _artifact: Path, _bundle: Path) -> object:
        """Refuse the attestation."""
        msg = "attestation rejected"
        raise ProverToolError(msg)

    with pytest.raises(ProverToolError, match=r"attestation rejected"):
        install_snapshot(
            install_options(case),
            install_hooks(case, setup_calls=calls, attestation=reject),
        )
    assert calls == [], "setup ran despite a rejected attestation"


def test_install_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    """A checksum list that disagrees with the manifest is refused."""
    case = make_case(tmp_path)
    path = case.assets_dir / CHECKSUMS_NAME
    write_file(path, path.read_bytes().replace(b"backend.tar.gz", b"backend.tar.gX"))
    with pytest.raises(ProverToolError, match=r"checksum"):
        install_snapshot(install_options(case), install_hooks(case))


def test_install_rejects_an_archive_that_disagrees_with_inventory(
    tmp_path: Path,
) -> None:
    """Archive members must match the published inventory."""
    case = make_case(tmp_path)
    backend = tar_bytes({
        "bin/kani-driver": (b"driver", 0o755),
        "bin/kani-compiler": (b"different", 0o755),
        "share/data.txt": (b"data", 0o644),
    })
    write_file(case.assets_dir / "backend.tar.gz", backend)
    rewrite_manifest_asset(case, "backend.tar.gz", backend)
    calls: list[Path] = []
    with pytest.raises(ProverToolError, match=r"different size|inventory"):
        install_snapshot(
            install_options(case),
            install_hooks(case, setup_calls=calls),
        )
    assert calls == [], "setup ran despite an inventory disagreement"


def test_install_rejects_a_mismatched_effective_compiler(tmp_path: Path) -> None:
    """A compiler probe that differs from the manifest fails closed."""
    case = make_case(tmp_path)
    calls: list[Path] = []

    def wrong(runtime: SnapshotRuntime) -> SetupEvidence:
        """Report a compiler that differs from the manifest."""
        return SetupEvidence(
            observed=ObservedToolchain(
                rustc_vv="rustc wrong",
                kani_compiler=case.manifest.toolchain.kani_compiler,
            ),
            toolchain_link_target=(
                runtime.layout.rustup_home / "toolchains" / runtime.toolchain
            ),
        )

    with pytest.raises(ProverToolError, match=r"rustc"):
        install_snapshot(
            install_options(case),
            install_hooks(case, setup_calls=calls, probe=wrong),
        )
    assert calls, "setup did not run before the compiler probe"
    assert load_active(snapshot_layout(case.pin, cache_home=tmp_path)) is None, (
        "a mismatched probe activated a generation"
    )


def test_install_repairs_a_corrupt_active_generation(tmp_path: Path) -> None:
    """A corrupt active generation is replaced without deleting it."""
    case = make_case(tmp_path)
    calls: list[Path] = []
    hooks = install_hooks(case, setup_calls=calls)
    first = install_snapshot(install_options(case), hooks)
    receipt = first.generation / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    second = install_snapshot(install_options(case), hooks)
    assert second.generation != first.generation, "repair reused the corrupt generation"
    assert first.generation.is_dir(), "repair deleted the previous generation"
    assert first.generation.name != active_generation(case), (
        "active pointer still names the corrupt generation"
    )
    assert len(calls) == 2, "repair did not run setup in a new generation"


def test_check_reports_the_active_generation(tmp_path: Path) -> None:
    """Checking returns the same validated identity installation did."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    checked = check_snapshot(install_options(case), install_hooks(case))
    assert checked == identity, "check reported a different identity"


def test_check_rejects_a_missing_active_pointer(tmp_path: Path) -> None:
    """Checking before installation is a clear error."""
    case = make_case(tmp_path)
    with pytest.raises(ProverToolError, match=r"not installed"):
        check_snapshot(install_options(case), install_hooks(case))


def test_install_stops_before_setup_when_the_deadline_expired(
    tmp_path: Path,
) -> None:
    """An expired budget prevents setup."""
    case = make_case(tmp_path)
    calls: list[Path] = []
    options = dc.replace(install_options(case), deadline_seconds=0.0)
    with pytest.raises(ProverToolError, match=r"deadline"):
        install_snapshot(options, install_hooks(case, setup_calls=calls))
    assert calls == [], "setup ran after the deadline expired"


def test_install_rejects_an_oversized_manifest(tmp_path: Path) -> None:
    """The manifest transport bound is enforced before parsing."""
    case = make_case(tmp_path)
    write_file(case.assets_dir / MANIFEST_NAME, b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ProverToolError, match=r"manifest.*limit"):
        install_snapshot(install_options(case), install_hooks(case))


def test_install_shares_the_remaining_budget_between_setup_and_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each external step receives the budget left when it starts."""
    case = make_case(tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    budgets: dict[str, float | None] = {}
    base = install_hooks(case)

    def record_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Record the setup budget, then spend part of it."""
        budgets["setup"] = timeout_seconds
        clock["now"] += 7.5
        base.setup(runtime, bundle, timeout_seconds)

    def record_probe(
        runtime: SnapshotRuntime,
        timeout_seconds: float | None,
    ) -> SetupEvidence:
        """Record the probe budget."""
        budgets["probe"] = timeout_seconds
        return base.probe(runtime, timeout_seconds)

    hooks = dc.replace(base, setup=record_setup, probe=record_probe)
    options = dc.replace(install_options(case), deadline_seconds=30.0)
    install_snapshot(options, hooks)

    assert budgets.keys() == {"setup", "probe"}, "both steps were not bounded"
    setup_budget = budgets["setup"]
    probe_budget = budgets["probe"]
    assert setup_budget is not None, "setup ran without a budget"
    assert probe_budget is not None, "probe ran without a budget"
    assert setup_budget == pytest.approx(30.0), (
        "setup did not start with the whole installation budget"
    )
    assert probe_budget == pytest.approx(setup_budget - 7.5), (
        "probe did not receive the budget left after setup"
    )


def test_validate_generation_measures_the_probe_budget_after_cache_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow receipt and cache checks do not stale the probe's budget."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=tmp_path,
        generation=active_generation(case),
    )
    order: list[str] = []
    real_validate = install_state.validate_cached_payloads

    def record_payloads(
        layout: SnapshotLayout,
        manifest: SnapshotManifest,
        *,
        checkpoint: cabc.Callable[[], None] | None = None,
    ) -> None:
        """Record the cache revalidation and forward the budget checkpoint."""
        order.append("payloads")
        real_validate(layout, manifest, checkpoint=checkpoint)

    monkeypatch.setattr(install_state, "validate_cached_payloads", record_payloads)
    base = install_hooks(case)

    def record_probe(
        runtime: SnapshotRuntime,
        timeout_seconds: float | None,
    ) -> SetupEvidence:
        """Record the probe."""
        order.append("probe")
        return base.probe(runtime, timeout_seconds)

    def remaining() -> float:
        """Report a freshly measured probe budget."""
        order.append("budget")
        return 5.0

    validate_generation(
        install_options(case),
        case.manifest,
        layout,
        record_probe,
        remaining,
    )
    assert order == ["payloads", "budget", "probe"], (
        "the probe budget was measured before the cache checks finished"
    )


def test_failed_repair_leaves_existing_downloads_untouched(tmp_path: Path) -> None:
    """A failed second install cannot replace validated cached evidence."""
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    downloads = snapshot_layout(case.pin, cache_home=tmp_path).downloads_dir
    before = {path.name: path.read_bytes() for path in downloads.iterdir()}
    # Force a repair so staging runs again instead of reusing the active
    # generation.
    (first.generation / "receipt.json").write_text("{}", encoding="utf-8")

    # A broken checksum manifest fails after the manifest and attestation have
    # been staged, which is exactly the window that must not disturb evidence.
    write_file(case.assets_dir / CHECKSUMS_NAME, b"not a checksum list\n")
    with pytest.raises(ProverToolError):
        install_snapshot(install_options(case), hooks)

    assert {path.name: path.read_bytes() for path in downloads.iterdir()} == before
    identity_dir = downloads.parent
    staging = [path for path in identity_dir.iterdir() if path.name.startswith(".")]
    assert staging == [], f"failed staging left temporary directories: {staging}"


def test_install_enforces_the_payload_transport_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload larger than the design bound never reaches setup."""
    case = make_case(tmp_path)
    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.install_transport.PAYLOAD_MAX_BYTES",
        16,
    )

    with pytest.raises(ProverToolError, match=r"backend\.tar\.gz.*transport limit"):
        install_snapshot(install_options(case), install_hooks(case))


def test_install_enforces_the_inventory_transport_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inventory document has its own smaller bound."""
    case = make_case(tmp_path)
    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.install_transport.INVENTORY_MAX_BYTES",
        16,
    )

    with pytest.raises(ProverToolError, match=r"inventory.*transport limit"):
        install_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_tampered_installed_backend_bytes(tmp_path: Path) -> None:
    """An installed backend file that no longer matches the inventory fails."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    installed = identity.launcher.parent.parent / "share" / "data.txt"
    installed.write_bytes(b"DATA")

    with pytest.raises(ProverToolError, match=r"wrong digest"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_tampered_installed_launcher_bytes(tmp_path: Path) -> None:
    """The installed launcher must still be the verified archive's bytes."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    identity.launcher.write_bytes(b"#!/bin/sh\necho tampered\n")

    with pytest.raises(ProverToolError, match=r"launcher"):
        check_snapshot(install_options(case), install_hooks(case))


def test_install_rejects_setup_that_loses_a_backend_member(
    tmp_path: Path,
) -> None:
    """Activation requires the installed tree to contain every member."""
    case = make_case(tmp_path)
    base = install_hooks(case)

    def lossy_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Run the normal setup, then drop one recorded member."""
        base.setup(runtime, bundle, timeout_seconds)
        (runtime.kani_dir / "share" / "data.txt").unlink()

    hooks = dc.replace(base, setup=lossy_setup)
    with pytest.raises(ProverToolError, match=r"missing recorded"):
        install_snapshot(install_options(case), hooks)


def test_install_rejects_a_tampered_toolchain_before_probing(
    tmp_path: Path,
) -> None:
    """Setup that installs different compiler bytes never reaches the probe."""
    case = make_case(tmp_path)
    base = install_hooks(case)
    probed: list[bool] = []

    def tampering_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Install the fixture toolchain, then alter the compiler."""
        base.setup(runtime, bundle, timeout_seconds)
        rustc = (
            runtime.layout.rustup_home
            / "toolchains"
            / runtime.toolchain
            / "bin"
            / "rustc"
        )
        rustc.write_bytes(RUSTC_BYTES.replace(b"printf", b"exec  "))

    def recording_probe(
        runtime: SnapshotRuntime,
        timeout_seconds: float | None,
    ) -> SetupEvidence:
        """Record that the probe ran, then delegate to the fixture probe."""
        probed.append(True)
        return base.probe(runtime, timeout_seconds)

    hooks = dc.replace(base, setup=tampering_setup, probe=recording_probe)
    with pytest.raises(ProverToolError, match=r"compiler.*does not match"):
        install_snapshot(install_options(case), hooks)
    assert not probed, "a rejected toolchain still reached the compiler probe"


def test_check_rejects_tampered_installed_toolchain_bytes(tmp_path: Path) -> None:
    """Reuse re-verifies the compiler the manifest recorded, not just its name."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    toolchain = identity.rustc.parent.parent
    identity.rustc.write_bytes(RUSTC_BYTES.replace(b"printf", b"exec  "))

    with pytest.raises(ProverToolError, match=r"compiler.*does not match"):
        check_snapshot(install_options(case), install_hooks(case))

    # A library the compiler loads is held to the same record.
    identity.rustc.write_bytes(RUSTC_BYTES)
    (toolchain / LIBRARY_PATH).write_bytes(b"tampered library\n")
    with pytest.raises(ProverToolError, match=r"library.*does not match"):
        check_snapshot(install_options(case), install_hooks(case))


def _launcher_files() -> dict[str, tuple[bytes, int]]:
    """Return the fixture launcher archive's files by relative path."""
    archive = next(data for role, _, _, data in payloads() if role == "launcher")
    files: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                msg = f"fixture launcher member '{member.name}' has no body"
                raise AssertionError(msg)
            relative = member.name.split("/", 1)[1]
            files[relative] = (handle.read(), member.mode)
    return files


def test_install_refuses_a_launcher_archive_without_the_launcher(
    tmp_path: Path,
) -> None:
    """Fresh installation cannot activate a generation it cannot execute."""
    case = make_case(tmp_path)
    files = _launcher_files()
    del files["bin/kani"]
    rewrite_manifest_asset(case, "launcher.tar.gz", tar_bytes(files))

    with pytest.raises(ProverToolError, match=r"launcher is missing"):
        install_snapshot(install_options(case), install_hooks(case))

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "an unusable generation was activated"


def test_install_refuses_a_non_executable_launcher(tmp_path: Path) -> None:
    """A launcher published without its execute bit is not activatable."""
    case = make_case(tmp_path)
    files = _launcher_files()
    launcher, _ = files["bin/kani"]
    files["bin/kani"] = (launcher, 0o644)
    rewrite_manifest_asset(case, "launcher.tar.gz", tar_bytes(files))

    with pytest.raises(
        ProverToolError,
        match=r"launcher is not an executable regular file",
    ):
        install_snapshot(install_options(case), install_hooks(case))

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "an unusable generation was activated"


def test_check_rejects_a_python_root_link_escaping_the_generation(
    tmp_path: Path,
) -> None:
    """A `pyroot` link must not redirect `PYTHONPATH` outside the generation."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    outside = tmp_path / "outside-python"
    outside.mkdir()
    (identity.launcher.parent.parent / "pyroot" / "escape").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ProverToolError, match=r"Python root link escapes"):
        check_snapshot(install_options(case), install_hooks(case))


def test_install_rejects_a_python_root_link_to_a_sibling_home(
    tmp_path: Path,
) -> None:
    """A link to setup's private home leaves the tree the record describes.

    The record holds link text, not the bytes a link reaches, so a target that
    stays inside the generation but outside the root would let execution reach
    content the record never describes.
    """
    case = make_case(tmp_path)
    base = install_hooks(case)

    def linking_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Install normally, then link the root to the generation's Kani home."""
        base.setup(runtime, bundle, timeout_seconds)
        (runtime.python_root / "sibling").symlink_to(
            runtime.kani_dir,
            target_is_directory=True,
        )

    hooks = dc.replace(base, setup=linking_setup)
    with pytest.raises(ProverToolError, match=r"Python root link escapes"):
        install_snapshot(install_options(case), hooks)

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, (
        "a Python root reaching outside its own tree was activated"
    )


def test_check_rejects_a_changed_python_root_link_target(tmp_path: Path) -> None:
    """A recorded link cannot be retargeted inside the root after installation.

    Retargeting a contained link changes what `PYTHONPATH` resolves without
    adding or removing any member, so the record must observe the link text.
    """
    case = make_case(tmp_path)
    base = install_hooks(case)

    def linking_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Install normally, then add one recorded link between root members."""
        base.setup(runtime, bundle, timeout_seconds)
        root = runtime.python_root
        (root / "versions" / "a").mkdir(parents=True)
        (root / "versions" / "b").mkdir()
        (root / "current").symlink_to("versions/a", target_is_directory=True)

    hooks = dc.replace(base, setup=linking_setup)
    identity = install_snapshot(install_options(case), hooks)
    link = identity.launcher.parent.parent / "pyroot" / "current"
    link.unlink()
    link.symlink_to("versions/b", target_is_directory=True)

    with pytest.raises(ProverToolError, match=r"do not match the record"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_a_python_root_that_is_a_symbolic_link(
    tmp_path: Path,
) -> None:
    """The Python root itself must be generation-private real content."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    pyroot = identity.launcher.parent.parent / "pyroot"
    outside = tmp_path / "outside-python"
    outside.mkdir()
    shutil.rmtree(pyroot)
    pyroot.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProverToolError, match=r"must not be a symbolic link"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_an_unrecorded_python_root_member(tmp_path: Path) -> None:
    """Setup may add the Python root, but only the content it recorded."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    injected = (
        identity.launcher.parent.parent / "pyroot" / "site" / "kani" / "injected.py"
    )
    injected.write_bytes(b"import os\n")

    with pytest.raises(ProverToolError, match=r"does not describe"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_a_modified_python_root_member(tmp_path: Path) -> None:
    """A recorded Python member must keep the bytes setup wrote."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    recorded = identity.launcher.parent.parent / "pyroot" / PYTHON_ROOT_FILE
    recorded.write_bytes(b'"""Tampered Kani Python payload."""\n')

    with pytest.raises(ProverToolError, match=r"do not match the record"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_a_generation_without_a_python_root_record(
    tmp_path: Path,
) -> None:
    """Reuse cannot treat a missing record as an empty Python root."""
    case = make_case(tmp_path)
    identity = install_snapshot(install_options(case), install_hooks(case))
    record = identity.generation / "pyroot.json"
    record.unlink()

    with pytest.raises(ProverToolError, match=r"no recorded setup content"):
        check_snapshot(install_options(case), install_hooks(case))


def test_check_rejects_a_same_size_backend_replacement(tmp_path: Path) -> None:
    """Cache reuse rehashes the backend, so an equal-size edit is still caught."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    backend = layout.downloads_dir / "backend.tar.gz"
    data = bytearray(backend.read_bytes())
    data[-1] ^= 0xFF
    backend.write_bytes(bytes(data))

    with pytest.raises(ProverToolError, match=r"backend\.tar\.gz.*digest"):
        check_snapshot(install_options(case), install_hooks(case))


def test_install_rejects_a_rustup_home_that_escapes_the_generation(
    tmp_path: Path,
) -> None:
    """A relocated rustup home cannot satisfy the toolchain containment rule."""
    case = make_case(tmp_path)
    base = install_hooks(case)

    def relocating_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Install normally, then replace the private home with an outside link."""
        base.setup(runtime, bundle, timeout_seconds)
        outside = tmp_path / "outside-rustup"
        runtime.layout.rustup_home.rename(outside)
        runtime.layout.rustup_home.symlink_to(outside, target_is_directory=True)

    hooks = dc.replace(base, setup=relocating_setup)
    with pytest.raises(ProverToolError, match=r"rustup home escapes its generation"):
        install_snapshot(install_options(case), hooks)

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "an escaping rustup home was activated"


def test_check_rejects_a_relocated_rustup_home(tmp_path: Path) -> None:
    """Reuse applies the same generation containment rule as installation."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    outside = tmp_path / "outside-rustup"
    layout.rustup_home.rename(outside)
    layout.rustup_home.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProverToolError, match=r"rustup home escapes its generation"):
        check_snapshot(install_options(case), install_hooks(case))


def test_launcher_copy_reports_progress_inside_a_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher member is copied in chunks that each reach the deadline."""
    monkeypatch.setattr(install_generation, "READ_CHUNK_SIZE", 16)
    source = tmp_path / "launcher"
    source.write_bytes(b"x" * 64)
    source.chmod(0o755)
    destination = tmp_path / "installed"
    calls: list[None] = []

    def checkpoint() -> None:
        """Expire the budget partway through the copy."""
        calls.append(None)
        if len(calls) > 2:
            msg = "deadline expired during launcher placement"
            raise ProverToolError(msg)

    with pytest.raises(ProverToolError, match=r"launcher placement"):
        install_generation.copy_launcher(source, destination, checkpoint)

    assert not destination.exists(), "an interrupted copy left a partial launcher"


def test_install_deadline_interrupts_launcher_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired budget stops launcher placement before activation."""
    case = make_case(tmp_path)
    state = {"started": False}
    real_copy = install_generation.copy_launcher

    def expire_during_copy(
        entry: Path,
        destination: Path,
        checkpoint: cabc.Callable[[], None] | None,
        budget: InstallBudget | None = None,
    ) -> None:
        """Mark placement as started, then let the real copy checkpoint."""
        state["started"] = True
        real_copy(entry, destination, checkpoint, budget)

    def clock() -> float:
        """Report an expired budget once placement has started."""
        return 10_000.0 if state["started"] else 0.0

    monkeypatch.setattr(install_generation, "READ_CHUNK_SIZE", 1)
    monkeypatch.setattr(install_generation, "copy_launcher", expire_during_copy)
    monkeypatch.setattr(time, "monotonic", clock)
    options = dc.replace(install_options(case), deadline_seconds=10.0)

    with pytest.raises(
        ProverToolError,
        match=r"deadline expired during launcher placement",
    ):
        install_snapshot(options, install_hooks(case))

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "an expired install activated a generation"


def test_install_deadline_interrupts_payload_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large copy is stopped by the budget while it is still running."""
    case = make_case(tmp_path)
    state = {"expired": False}
    real_stream = install_copy.stream_bounded

    def expire_during_copy(
        reader: typ.BinaryIO,
        writer: typ.BinaryIO,
        file: install_copy.TransportFile,
        checkpoint: cabc.Callable[[], None] | None = None,
    ) -> int:
        """Mark the budget expired, then let the real copy checkpoint."""
        state["expired"] = True
        return real_stream(reader, writer, file, checkpoint)

    def clock() -> float:
        """Report an expired budget once copying has started."""
        return 10_000.0 if state["expired"] else 0.0

    monkeypatch.setattr(install_copy, "stream_bounded", expire_during_copy)
    monkeypatch.setattr(time, "monotonic", clock)
    options = dc.replace(install_options(case), deadline_seconds=10.0)

    with pytest.raises(ProverToolError, match=r"deadline expired during"):
        install_snapshot(options, install_hooks(case))

    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "an expired install activated a generation"


def test_install_deadline_bounds_a_stalled_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write stalled on a hung filesystem cannot outlive the budget.

    A blocking `fsync` never reaches a checkpoint, so only the supervision of
    the staging copy can end the wait. The test uses a real clock and a short
    budget, so a regression that waits for the stalled write to finish shows up
    as a test timeout rather than a false pass.
    """
    case = make_case(tmp_path)
    release = threading.Event()
    stalled: list[int] = []

    def hanging_fsync(descriptor: int) -> None:
        """Block like a hung filesystem until the test releases the worker."""
        stalled.append(descriptor)
        release.wait(30)

    monkeypatch.setattr(install_copy.os, "fsync", hanging_fsync)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(ProverToolError, match=r"deadline expired during"):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled, "the staged write never reached its stalled flush"
    assert elapsed < 10.0, f"the stalled write outlived the budget: {elapsed:.1f}s"
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    assert load_active(layout) is None, "a stalled install activated a generation"


def test_pointer_is_not_published_when_the_budget_expires(tmp_path: Path) -> None:
    """An expiry before publication leaves no record and no pending document.

    Installation checks the budget after the pending record is durable and
    immediately before the publication link, so the only visible states are
    the previous record and the complete new one.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)

    with pytest.raises(ProverToolError, match=r"pointer publication"):
        install_state.write_active(
            layout,
            new_generation_name(),
            budget=_ExpiringBudget("pointer publication"),
        )

    assert load_active(layout) is None, "an expired budget published a pointer"
    leftovers = list(layout.activations_dir.glob(".pending-*"))
    assert not leftovers, f"an expired budget left temporary records: {leftovers}"


def test_install_deadline_stops_before_publishing_payloads(
    tmp_path: Path,
) -> None:
    """An expired budget cannot publish verified payloads into the cache.

    Publication replaces shared files one at a time, so the budget is checked
    before each replacement instead of inside a supervised worker that could
    keep publishing after the installation reported failure.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)

    with pytest.raises(ProverToolError, match=r"payload publication"):
        install_transport.stage_assets(
            install_options(case),
            layout,
            install_hooks(case),
            budget=_ExpiringBudget("payload publication"),
        )

    published = (
        sorted(path.name for path in layout.downloads_dir.iterdir())
        if layout.downloads_dir.is_dir()
        else []
    )
    assert published == [], f"an expired budget published payloads: {published}"
    leftovers = list(layout.identity_dir.glob(".staging-*"))
    assert not leftovers, f"an expired staging left directories: {leftovers}"


def test_install_deadline_bounds_a_stalled_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read stalled on a hung filesystem cannot hold the lock past the budget.

    The reuse path reads the active pointer and the cached manifest while
    holding the identity lock. Supervision abandons such a read, which mutates
    nothing, so a later install is refused by the budget instead of waiting for
    the filesystem.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    generation = load_active(layout)
    assert generation is not None, "the first install activated no generation"
    release = threading.Event()
    stalled: list[str] = []
    real_manifest = install_module.active_manifest

    def stalling_manifest(
        pin: SnapshotPin,
        target: SnapshotLayout,
    ) -> SnapshotManifest:
        """Block like a hung read until the test releases the worker."""
        stalled.append("manifest")
        release.wait(30)
        return real_manifest(pin, target)

    monkeypatch.setattr(install_module, "active_manifest", stalling_manifest)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(ProverToolError, match=r"deadline expired during"):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled, "the reuse path never read the manifest"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert load_active(layout) == generation, (
        "a stall during reuse changed the active pointer"
    )


def test_activation_reports_unconfirmed_durability_after_the_pointer_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed directory flush is reported as durability, not as a failure.

    The link has already made the generation visible, so reporting a
    publication failure would be untrue. The error names the generation and
    the pointer is left naming it.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    generation = new_generation_name()
    real_flush = durability.fsync_directory

    def failing_flush(path: Path) -> None:
        """Fail the identity-directory flush, as a broken filesystem would."""
        if path == layout.identity_dir:
            msg = "input/output error"
            raise OSError(msg)
        real_flush(path)

    monkeypatch.setattr(durability, "fsync_directory", failing_flush)

    with pytest.raises(ProverToolError, match=r"active but its activation"):
        install_state.write_active(layout, generation)

    pointer = load_active(layout)
    assert pointer is not None, "the generation is not visible after the failure"
    assert pointer.generation == generation, "the pointer names another generation"
    leftovers = list(layout.activations_dir.glob(".pending-*"))
    assert not leftovers, f"a failed flush left temporary records: {leftovers}"


def test_activation_flush_timeout_is_reported_as_durability(
    tmp_path: Path,
) -> None:
    """An expiry during the post-publication flush does not claim publication failed."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    generation = new_generation_name()

    with pytest.raises(ProverToolError, match=r"active but its activation"):
        install_state.write_active(
            layout,
            generation,
            budget=_ExpiringSupervision("pointer directory flush"),
        )

    pointer = load_active(layout)
    assert pointer is not None, "the generation is not visible after the expiry"
    assert pointer.generation == generation, "the pointer names another generation"


def test_payload_publication_reports_an_unknown_outcome_when_it_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled replacement is abandoned and reported as indeterminate.

    The marker made durable before the first replacement is what makes the
    half-published cache visible as incomplete, so a reader cannot mistake it
    for a valid cache even though the abandoned worker may still finish.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    release = threading.Event()
    stalled: list[str] = []
    real_replace = publication._replace_verified

    def stalling_replace(source: Path, target: Path) -> None:
        """Block like a hung filesystem until the test releases the worker."""
        stalled.append(target.name)
        release.wait(30)
        real_replace(source, target)

    monkeypatch.setattr(publication, "_replace_verified", stalling_replace)
    budget = Deadline(time.monotonic() + 1.0)
    started = time.monotonic()
    try:
        with pytest.raises(ProverToolError, match=r"outcome is unknown"):
            install_transport.stage_assets(
                install_options(case),
                layout,
                install_hooks(case),
                budget=budget,
            )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled, "publication never reached a payload replacement"
    assert elapsed < 10.0, (
        f"the stalled publication outlived the budget: {elapsed:.1f}s"
    )
    markers = layout.publication_markers()
    assert len(markers) == 1, (
        f"an unresolved publication left {len(markers)} markers for readers"
    )

    # A later install revalidates the cache and clears the marker, so the
    # indeterminate outcome is recoverable rather than a dead end.
    install_snapshot(install_options(case), install_hooks(case))
    assert not layout.publication_markers(), (
        "a completed repair left the publication marker behind"
    )
    check_snapshot(install_options(case), install_hooks(case))


def test_check_refuses_a_cache_whose_publication_did_not_finish(
    tmp_path: Path,
) -> None:
    """A carrying marker makes the cache unreadable until a repair completes."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    layout.publication_marker("test-1").write_text("publishing\n", encoding="ascii")

    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), install_hooks(case))

    install_snapshot(install_options(case), install_hooks(case))
    assert not layout.publication_markers(), (
        "the repair did not clear the publication marker"
    )
    check_snapshot(install_options(case), install_hooks(case))


def test_reuse_cannot_succeed_while_the_activation_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after an unconfirmed activation keeps failing until a flush works.

    The first install publishes the activation record and then fails its
    directory flush.
    Every retry must reissue that flush and report the durability failure
    instead of validating the generation and reporting a successful reuse.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_flush = durability.fsync_directory
    flushes = {"identity": 0}
    # The marker write and the payload-directory flush each flush the identity
    # directory before activation; the third one is the activation flush.
    failing = {"from": 3}

    def flaky_flush(path: Path) -> None:
        """Fail identity-directory flushes from the second one onwards."""
        if path == layout.identity_dir:
            flushes["identity"] += 1
            if flushes["identity"] >= failing["from"]:
                msg = "input/output error"
                raise OSError(msg)
        real_flush(path)

    monkeypatch.setattr(durability, "fsync_directory", flaky_flush)
    monkeypatch.setattr(install_generation, "fsync_directory", flaky_flush)

    with pytest.raises(ProverToolError, match=r"active but its activation"):
        install_snapshot(install_options(case), install_hooks(case))
    pointer = load_active(layout)
    assert pointer is not None, "the pointer was not published before the flush"
    generation = pointer.generation

    with pytest.raises(ProverToolError, match=r"active but its activation"):
        install_snapshot(install_options(case), install_hooks(case))

    failing["from"] = 99
    identity = install_snapshot(install_options(case), install_hooks(case))
    assert identity.generation.name == generation, (
        "a retry installed another generation"
    )
    reused = load_active(layout)
    assert reused is not None, "a successful reuse left no activation record"
    assert reused.generation == pointer.generation, (
        "a successful reuse moved the pointer to another generation"
    )


def test_install_does_not_activate_when_the_payload_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached payload entries must be durable before the pointer may name them."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_flush = publication.flush_directory

    def failing_flush(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "directory flush",
    ) -> None:
        """Fail the downloads flush, as a broken filesystem would."""
        if path == layout.downloads_dir:
            msg = "failed to flush snapshot directory 'downloads'"
            raise ProverToolError(msg)
        real_flush(path, budget=budget, phase=phase)

    monkeypatch.setattr(publication, "flush_directory", failing_flush)

    with pytest.raises(ProverToolError, match=r"downloads"):
        install_snapshot(install_options(case), install_hooks(case))

    assert load_active(layout) is None, "an unflushed cache was activated"


def test_install_does_not_activate_when_the_generation_entry_is_unflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generation directory entry must be durable before activation."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_flush = install_module.flush_directory

    def failing_flush(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "directory flush",
    ) -> None:
        """Fail the generations-directory flush, as a broken filesystem would."""
        if path == layout.generation_dir.parent:
            msg = "failed to flush snapshot directory 'generations'"
            raise ProverToolError(msg)
        real_flush(path, budget=budget, phase=phase)

    monkeypatch.setattr(install_module, "flush_directory", failing_flush)

    with pytest.raises(ProverToolError, match=r"generations"):
        install_snapshot(install_options(case), install_hooks(case))

    assert load_active(layout) is None, "an unflushed generation was activated"


def test_an_abandoned_activation_cannot_displace_a_later_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link abandoned by its caller cannot outrank a later activation.

    The abandoned worker holds the sequence number it observed before its
    caller timed out. A later activation takes that number first, so releasing
    the worker can only find it already created; the active record still names
    the later generation and the cache stays valid.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    (first.generation / "receipt.json").write_text("{}", encoding="utf-8")
    second = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    real_link = os.link
    calls = {"count": 0}

    def stalling_link(source: Path, target: Path) -> None:
        """Hold the first link across another activation, then retry it."""
        calls["count"] += 1
        if calls["count"] != 1:
            real_link(source, target)
            return
        # Preserve the inode the abandoned worker is publishing, so the test
        # exercises the create-only rule rather than the removal of the
        # worker's private document.
        preserved = layout.activations_dir / ".preserved-record"
        real_link(source, preserved)
        try:
            release.wait(30)
            real_link(preserved, target)
        finally:
            finished.set()

    monkeypatch.setattr(activation.os, "link", stalling_link)

    with pytest.raises(ProverToolError, match=r"outcome is unknown"):
        activation.write_active(
            layout,
            first.generation.name,
            budget=Deadline(time.monotonic() + 0.5),
        )
    # The later activation takes the sequence number the abandoned worker
    # observed, because that worker never published its record.
    activation.write_active(layout, second.generation.name)

    try:
        release.set()
        assert finished.wait(10), "the abandoned activation never resumed"
    finally:
        release.set()

    pointer = load_active(layout)
    assert pointer is not None, "no activation record survived"
    assert pointer.generation == second.generation.name, (
        f"an abandoned activation displaced {second.generation.name} "
        f"with {pointer.generation}"
    )
    check_snapshot(install_options(case), hooks)


def _completion(layout: SnapshotLayout) -> int:
    """Return the completion watermark, requiring a completed installation."""
    watermark = completion.completion_watermark(layout)
    assert watermark > 0, "the install recorded no completion"
    return watermark


def test_an_abandoned_completion_cannot_clear_a_repair_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed publication removes only its own marker, never a repair's.

    The abandoned completion resumes after a repair has written its own marker
    and been interrupted. Because no marker name is reused, the repair's
    marker survives and readers still see the cache as unfinished.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    names = tuple(sorted(path.name for path in layout.downloads_dir.iterdir()))
    marker_a = layout.publication_marker("abandoned")
    marker_b = layout.publication_marker("repair")

    def restage(label: str) -> pathlib.Path:
        """Copy the cached payloads into a private staging directory."""
        staging = layout.identity_dir / f".staging-test-{label}"
        staging.mkdir(mode=0o700)
        for name in names:
            shutil.copy2(layout.downloads_dir / name, staging / name)
        return staging

    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_unlink = pathlib.Path.unlink

    def stalling_unlink(self: Path, *, missing_ok: bool = False) -> None:
        """Hold the abandoned completion's unlink of its own marker."""
        is_own_marker = (
            self == marker_a
            and threading.current_thread() is not threading.main_thread()
        )
        if not is_own_marker:
            real_unlink(self, missing_ok=missing_ok)
            return
        stalled.set()
        release.wait(30)
        try:
            real_unlink(self, missing_ok=missing_ok)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "unlink", stalling_unlink)

    with pytest.raises(ProverToolError, match=r"completion marker"):
        publication.publish_staged(
            publication.Publication(
                staging=restage("a"),
                downloads=layout.downloads_dir,
                marker=marker_a,
                stale_markers=(),
                names=names,
                base_sequence=activation.highest_sequence(layout),
            ),
            budget=Deadline(time.monotonic() + 0.5),
        )
    assert stalled.is_set(), "the publication completion never stalled"
    assert marker_a.exists(), "the abandoned publication lost its marker"

    with pytest.raises(ProverToolError, match=r"completion marker"):
        publication.publish_staged(
            publication.Publication(
                staging=restage("b"),
                downloads=layout.downloads_dir,
                marker=marker_b,
                stale_markers=(marker_a,),
                names=names,
                base_sequence=activation.highest_sequence(layout),
            ),
            budget=_ExpiringSupervision("publication completion"),
        )

    try:
        release.set()
        assert finished.wait(10), "the abandoned completion never resumed"
    finally:
        release.set()

    assert marker_b.exists(), "an abandoned completion removed a later repair's marker"
    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), hooks)

    install_snapshot(install_options(case), hooks)
    assert not layout.publication_markers(), (
        "the completed repair left a publication marker behind"
    )
    check_snapshot(install_options(case), hooks)


def test_a_late_completion_write_cannot_retire_a_repair_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watermark that lands late cannot hide a repair that began after it.

    Install A links its activation record and is abandoned while the
    supervised completion write is stalled. Repair B then observes that
    record, publishes its own marker, and is interrupted before it replaces
    any payload. When A's worker resumes and advances the watermark to its own
    sequence, B's marker must still name an installation that began before
    that completion, so readers must refuse the cache.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    names = tuple(sorted(path.name for path in layout.downloads_dir.iterdir()))

    release = threading.Event()
    stalled = threading.Event()
    finished = threading.Event()
    real_replace = pathlib.Path.replace

    def stalling_replace(self: Path, target: Path) -> None:
        """Hold the completion write's rename until the repair is interrupted."""
        is_watermark = (
            self.name.startswith(".completed-")
            and threading.current_thread().name == "snapshot completion watermark"
        )
        if not is_watermark:
            real_replace(self, target)
            return
        stalled.set()
        release.wait(30)
        try:
            real_replace(self, target)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "replace", stalling_replace)

    # Install A publishes its activation record, then loses its deadline in
    # the completion write. The abandoned worker keeps running and lands the
    # watermark only after the repair below has already begun.
    with pytest.raises(ProverToolError, match=r"completion watermark"):
        activation.write_active(
            layout,
            first.generation.name,
            budget=Deadline(time.monotonic() + 0.5),
        )
    assert stalled.is_set(), "the completion write was never attempted"

    # Repair B observes A's record before writing its marker, then is
    # interrupted as it publishes the first payload.
    staging = layout.identity_dir / ".staging-test-late-completion"
    staging.mkdir(mode=0o700)
    for name in names:
        shutil.copy2(layout.downloads_dir / name, staging / name)
    with pytest.raises(ProverToolError, match=r"outcome is unknown"):
        publication.publish_staged(
            install_transport._publication(
                layout,
                staging,
                list(names),
                Deadline(time.monotonic() + 5.0),
            ),
            budget=_ExpiringSupervision("payload publication"),
        )

    try:
        release.set()
        assert finished.wait(10), "the abandoned completion write never resumed"
    finally:
        release.set()

    assert layout.publication_markers(), "the interrupted repair left no marker"
    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), hooks)

    install_snapshot(install_options(case), hooks)
    assert not layout.publication_markers(), (
        "the completed repair left a publication marker behind"
    )
    check_snapshot(install_options(case), hooks)


def test_launcher_mode_is_durable_before_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executable bit is applied before the launcher bytes are flushed."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    source = layout.launcher_dir / "bin"
    source.mkdir(parents=True)
    launcher = source / "kani"
    launcher.write_bytes(b"#!/bin/sh\n")
    launcher.chmod(0o755)
    runtime = SnapshotRuntime(
        layout=layout,
        toolchain="1.85.0-x86_64-unknown-linux-gnu",
        backend_version="0.56.0",
    )
    events: list[tuple[str, int]] = []
    real_fchmod = os.fchmod
    real_fsync = os.fsync

    def record_fchmod(descriptor: int, mode: int) -> None:
        """Record the mode change, then apply it."""
        events.append(("chmod", descriptor))
        real_fchmod(descriptor, mode)

    def record_fsync(descriptor: int) -> None:
        """Record each durable flush."""
        events.append(("fsync", descriptor))
        real_fsync(descriptor)

    monkeypatch.setattr(install_generation.os, "fchmod", record_fchmod)
    monkeypatch.setattr(install_generation.os, "fsync", record_fsync)

    install_generation.install_launcher_binaries(runtime, layout)

    assert runtime.launcher.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(runtime.launcher.stat().st_mode) == 0o755, (
        "the installed launcher lost its executable bit"
    )
    assert events[0][0] == "chmod", "the launcher bytes were flushed before its mode"
    assert events[1] == ("fsync", events[0][1]), (
        "the executable bit was not made durable with the bytes it guards"
    )


def test_install_does_not_activate_when_a_launcher_directory_is_unflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested launcher directory must be durable before activation."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_flush = install_generation.flush_directory
    flushed = {"bin": 0}

    def failing_flush(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "directory flush",
    ) -> None:
        """Fail the generation sweep's flush of the launcher directory."""
        if path.name == "bin":
            flushed["bin"] += 1
            if flushed["bin"] >= 2:
                msg = "failed to flush snapshot directory 'bin'"
                raise ProverToolError(msg)
        real_flush(path, budget=budget, phase=phase)

    monkeypatch.setattr(install_generation, "flush_directory", failing_flush)

    with pytest.raises(ProverToolError, match=r"'bin'"):
        install_snapshot(install_options(case), install_hooks(case))

    assert flushed["bin"] >= 2, "the generation sweep never flushed the launcher bin"
    assert load_active(layout) is None, "an unflushed launcher was activated"


def test_install_deadline_bounds_a_stalled_staging_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled staging-directory creation cannot hold the lock past budget."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled: list[str] = []
    real_mkdir = pathlib.Path.mkdir

    def stalling_mkdir(
        self: Path,
        mode: int = 0o777,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Block a private staging creation like a hung filesystem."""
        if not self.name.startswith(".staging-"):
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
            return
        stalled.append(self.name)
        release.wait(30)
        try:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "mkdir", stalling_mkdir)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during staging directory creation",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled, "the staging creation was never attempted"
    assert elapsed < 10.0, f"the stalled creation outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned creation never resumed"
    assert load_active(layout) is None, "a stalled staging created an active pointer"
    leftovers = list(layout.identity_dir.glob(".staging-*"))
    assert len(leftovers) == 1, f"unexpected staging leftovers: {leftovers}"
    assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o700, (
        "an abandoned staging directory is not private"
    )


def test_install_deadline_defers_a_stalled_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stalled cleanup is deferred and logged instead of holding the lock."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_rmtree = shutil.rmtree

    def stalling_rmtree(
        path: str | Path,
        *,
        ignore_errors: bool = False,
    ) -> None:
        """Block removal of this run's private staging tree."""
        target = pathlib.Path(path)
        is_staging = target.parent == layout.identity_dir and target.name.startswith(
            ".staging-"
        )
        if not is_staging:
            real_rmtree(target, ignore_errors=ignore_errors)
            return
        stalled.set()
        release.wait(30)
        try:
            real_rmtree(target, ignore_errors=ignore_errors)
        finally:
            finished.set()

    monkeypatch.setattr(cleanup.shutil, "rmtree", stalling_rmtree)
    options = dc.replace(install_options(case), deadline_seconds=2.0)
    started = time.monotonic()
    with caplog.at_level(
        logging.DEBUG,
        logger="rust_prover_tools.kani_snapshot.cleanup",
    ):
        try:
            with pytest.raises(ProverToolError, match=r"deadline expired"):
                install_snapshot(options, install_hooks(case))
            elapsed = time.monotonic() - started
        finally:
            release.set()

    assert stalled.is_set(), "the staging cleanup was never attempted"
    assert elapsed < 10.0, f"the stalled cleanup outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned cleanup never resumed"
    assert load_active(layout) is None, "a deferred cleanup activated a generation"
    assert "staging cleanup deferred" in caplog.text, (
        "the deferred cleanup was not recorded"
    )


def test_a_recorded_toolchain_library_is_flushed_before_its_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required library is durable before the entries that name it.

    Flushing the compiler alone would leave the library it loads reachable
    only through directory entries that a crash could lose. The library's file
    flush must therefore precede the flush of its parent and of every ancestor
    up to the toolchain root.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    runtime = runtime_for(layout, case.manifest)
    toolchain = layout.rustup_home / "toolchains" / runtime.toolchain
    library = toolchain / LIBRARY_PATH
    events: list[tuple[str, Path]] = []
    real_fsync_file = install_generation.fsync_file
    real_fsync_directory = durability.fsync_directory

    def record_file(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "file flush",
    ) -> None:
        """Record one file flush, then perform it."""
        events.append(("file", path))
        real_fsync_file(path, budget=budget, phase=phase)

    def record_directory(path: Path) -> None:
        """Record one directory flush, then perform it."""
        events.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(install_generation, "fsync_file", record_file)
    monkeypatch.setattr(durability, "fsync_directory", record_directory)

    install_generation.flush_generation_entries(runtime, layout, case.manifest)

    library_position = events.index(("file", library))
    ancestors = (library.parent, toolchain / "lib", toolchain)
    for directory in ancestors:
        assert library_position < events.index(("directory", directory)), (
            f"{directory} was flushed before the library it names"
        )


def test_install_does_not_activate_when_a_python_root_member_is_unflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unflushed Python payload prevents activation, not just a warning."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_fsync_file = durability.fsync_file

    def failing_file_flush(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "file flush",
    ) -> None:
        """Fail the flush of the fixture's recorded Python payload."""
        if path.name == "__init__.py" and "pyroot" in path.parts:
            msg = f"failed to flush snapshot file '{path.name}'"
            raise ProverToolError(msg)
        real_fsync_file(path, budget=budget, phase=phase)

    monkeypatch.setattr(durability, "fsync_file", failing_file_flush)

    with pytest.raises(ProverToolError, match=r"__init__\.py"):
        install_snapshot(install_options(case), install_hooks(case))

    assert load_active(layout) is None, "an unflushed Python root was activated"


def test_install_deadline_bounds_a_stalled_staged_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged manifest read cannot hold the identity lock past the budget."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_read = pathlib.Path.read_bytes

    def stalling_read(self: Path) -> bytes:
        """Block the staged manifest read like a hung filesystem."""
        is_staged_manifest = self.name == MANIFEST_NAME and self.parent.name.startswith(
            ".staging-"
        )
        if not is_staged_manifest:
            return real_read(self)
        stalled.set()
        release.wait(30)
        try:
            return real_read(self)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "read_bytes", stalling_read)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during manifest read",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the staged manifest was never read"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned read never resumed"
    assert load_active(layout) is None, "a stalled manifest read activated a generation"
    published = (
        sorted(path.name for path in layout.downloads_dir.iterdir())
        if layout.downloads_dir.is_dir()
        else []
    )
    assert published == [], f"a stalled manifest read published payloads: {published}"


def test_activation_defers_a_stalled_pending_record_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stalled removal of this call's pending record is deferred, not awaited."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    generation = new_generation_name()
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_unlink = pathlib.Path.unlink

    def stalling_unlink(self: Path, *, missing_ok: bool = False) -> None:
        """Block the private record cleanup like a hung filesystem."""
        is_pending = self.name.startswith(".pending-") and (
            threading.current_thread() is not threading.main_thread()
        )
        if not is_pending:
            real_unlink(self, missing_ok=missing_ok)
            return
        stalled.set()
        release.wait(30)
        try:
            real_unlink(self, missing_ok=missing_ok)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "unlink", stalling_unlink)
    started = time.monotonic()
    try:
        with (
            caplog.at_level(
                logging.DEBUG,
                logger="rust_prover_tools.kani_snapshot.cleanup",
            ),
            pytest.raises(
                ProverToolError,
                match=r"deadline expired|active but its activation",
            ),
        ):
            activation.write_active(
                layout,
                generation,
                budget=Deadline(time.monotonic() + 5.0),
            )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the private record cleanup never stalled"
    assert elapsed < 20.0, f"the stalled cleanup outlived the budget: {elapsed:.1f}s"
    assert "activation record cleanup deferred" in caplog.text, (
        "the deferred cleanup was not recorded"
    )
    assert finished.wait(10), "the abandoned cleanup never resumed"
    pointer = load_active(layout)
    assert pointer is not None, "the activation record was lost"
    assert pointer.generation == generation, (
        "the activation record names another generation"
    )


def test_installation_fails_when_a_stale_publication_marker_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker that keeps the cache refused is an installation failure."""
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    stale = layout.publication_marker("stale")
    stale.write_text("", encoding="utf-8")
    real_unlink = pathlib.Path.unlink

    def failing_unlink(self: Path, *, missing_ok: bool = False) -> None:
        """Refuse to remove the stale marker, as a broken cache would."""
        if self == stale:
            msg = "operation not permitted"
            raise PermissionError(msg)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", failing_unlink)

    with pytest.raises(ProverToolError, match=r"could not be removed"):
        install_snapshot(install_options(case), hooks)

    pointer = load_active(layout)
    assert pointer is not None, "the previous activation record disappeared"
    assert pointer.generation == first.generation.name, (
        "a failed repair changed the active generation"
    )
    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), hooks)


def _record_flushes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Record generation flushes in the order they are issued."""
    events: list[tuple[str, Path]] = []
    real_fsync_file = install_generation.fsync_file
    real_fsync_directory = durability.fsync_directory

    def record_file(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "file flush",
    ) -> None:
        """Record one file flush, then perform it."""
        events.append(("file", path))
        real_fsync_file(path, budget=budget, phase=phase)

    def record_directory(path: Path) -> None:
        """Record one directory flush, then perform it."""
        events.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(install_generation, "fsync_file", record_file)
    monkeypatch.setattr(durability, "fsync_directory", record_directory)
    return events


def test_a_backend_inventory_member_is_flushed_before_its_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend inventory member is flushed like a named executable.

    Setup extracts the backend with `tar`, so a member the manifest never
    names individually, such as `share/data.txt`, is still payload the
    activation record depends on. Its bytes and the directory entries naming
    it must be durable before activation.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    runtime = runtime_for(layout, case.manifest)
    member = runtime.kani_dir / "share" / "data.txt"
    events = _record_flushes(monkeypatch)

    install_generation.flush_generation_entries(runtime, layout, case.manifest)

    member_position = events.index(("file", member))
    for directory in (member.parent, runtime.kani_dir):
        assert member_position < events.index(("directory", directory)), (
            f"{directory} was flushed before the inventory member it names"
        )


def test_install_does_not_activate_when_a_backend_member_is_unflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unflushed inventory member prevents activation, not just a warning."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    real_fsync_file = install_generation.fsync_file

    def failing_file_flush(
        path: Path,
        *,
        budget: InstallBudget | None = None,
        phase: str = "file flush",
    ) -> None:
        """Fail the flush of the fixture's backend inventory member."""
        if path.name == "data.txt" and path.parent.name == "share":
            msg = f"failed to flush snapshot file '{path.name}'"
            raise ProverToolError(msg)
        real_fsync_file(path, budget=budget, phase=phase)

    monkeypatch.setattr(install_generation, "fsync_file", failing_file_flush)

    with pytest.raises(ProverToolError, match=r"data\.txt"):
        install_snapshot(install_options(case), install_hooks(case))

    assert load_active(layout) is None, "an unflushed backend member was activated"


def _component_case(
    tmp_path: Path,
) -> tuple[SnapshotCase, SnapshotLayout, SnapshotRuntime, Path, SnapshotManifest]:
    """Install the fixture and record a library outside `lib/rustlib`."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    runtime = runtime_for(layout, case.manifest)
    toolchain = layout.rustup_home / "toolchains" / runtime.toolchain
    extra = toolchain / "lib" / "extra.rlib"
    data = b"extra fixture library\n"
    extra.write_bytes(data)
    manifest = dc.replace(
        case.manifest,
        toolchain=dc.replace(
            case.manifest.toolchain,
            libraries=(FileRecord(path="lib/extra.rlib", sha256=sha256_hex(data)),),
        ),
    )
    return case, layout, runtime, toolchain, manifest


def test_a_component_directory_is_flushed_without_a_recorded_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The component record's `lib/rustlib` parent is required on its own.

    Flushing only each recorded library's ancestor chain leaves a generation
    whose component record sits in an unflushed directory whenever no recorded
    library happens to share it.
    """
    _, layout, runtime, toolchain, manifest = _component_case(tmp_path)
    extra = toolchain / "lib" / "extra.rlib"
    events = _record_flushes(monkeypatch)

    install_generation.flush_generation_entries(runtime, layout, manifest)

    file_position = events.index(("file", extra))
    for directory in (toolchain / "lib" / "rustlib", toolchain / "lib", toolchain):
        assert file_position < events.index(("directory", directory)), (
            f"{directory} was flushed before the library it names"
        )


def test_generation_flush_fails_when_the_component_directory_is_unflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component directory that cannot be flushed is a fatal sweep failure."""
    _, layout, runtime, toolchain, manifest = _component_case(tmp_path)
    rustlib = toolchain / "lib" / "rustlib"
    real_fsync_directory = durability.fsync_directory

    def failing_directory_flush(path: Path) -> None:
        """Fail the flush of the directory holding the component record."""
        if path == rustlib:
            msg = f"failed to flush snapshot directory '{path.name}'"
            raise ProverToolError(msg)
        real_fsync_directory(path)

    monkeypatch.setattr(durability, "fsync_directory", failing_directory_flush)

    with pytest.raises(ProverToolError, match=r"rustlib"):
        install_generation.flush_generation_entries(runtime, layout, manifest)


def test_install_deadline_bounds_a_stalled_generation_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled generation-directory creation cannot hold the lock past budget."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_mkdir = pathlib.Path.mkdir

    def stalling_mkdir(
        self: Path,
        mode: int = 0o777,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Block a private generation creation like a hung filesystem."""
        is_generation = (
            self.parent.name == "generations"
            and threading.current_thread() is not threading.main_thread()
        )
        if not is_generation:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
            return
        stalled.set()
        release.wait(30)
        try:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "mkdir", stalling_mkdir)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during generation directory creation",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the generation creation was never attempted"
    assert elapsed < 10.0, f"the stalled creation outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned creation never resumed"
    assert load_active(layout) is None, "a stalled generation creation activated"


def test_install_deadline_bounds_a_stalled_activation_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled record listing cannot hold the identity lock past the budget."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_iterdir = pathlib.Path.iterdir

    def stalling_iterdir(self: Path) -> cabc.Iterator[Path]:
        """Block the activation record listing like a hung filesystem.

        Installation reads the pointer once under supervision before it
        stages payloads. That read must succeed normally, so only the listing
        that allocates a sequence number stalls: by then this activation's
        private `.pending-` document is already in the directory.
        """
        is_activation_listing = (
            self == layout.activations_dir
            and threading.current_thread() is not threading.main_thread()
        )
        if not is_activation_listing:
            return real_iterdir(self)
        has_pending = any(
            entry.name.startswith(".pending-") for entry in real_iterdir(self)
        )
        if not has_pending:
            return real_iterdir(self)
        stalled.set()
        release.wait(30)
        try:
            return real_iterdir(self)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "iterdir", stalling_iterdir)
    options = dc.replace(install_options(case), deadline_seconds=2.0)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during activation record enumeration",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the activation records were never listed"
    assert elapsed < 10.0, f"the stalled listing outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned listing never resumed"
    assert load_active(layout) is None, "a stalled listing activated a generation"


def test_install_deadline_bounds_a_stalled_identity_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache's own directory creation is bounded, not just its children.

    Installation makes the identity directory before it can open the lock
    file. A creation stalled on a hung filesystem must be abandoned like any
    other blocking step, because the identity lock is not held yet and the
    directory's existence is idempotent.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_mkdir = pathlib.Path.mkdir

    def stalling_mkdir(
        self: Path,
        mode: int = 0o777,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Block the identity directory creation like a hung filesystem."""
        is_identity = (
            self == layout.identity_dir
            and threading.current_thread() is not threading.main_thread()
        )
        if not is_identity:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
            return
        stalled.set()
        release.wait(30)
        try:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "mkdir", stalling_mkdir)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during identity directory creation",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the identity directory creation was never attempted"
    assert elapsed < 10.0, f"the stalled creation outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned creation never resumed"
    assert not layout.downloads_dir.exists(), "a stalled creation published payloads"


def test_install_lock_wait_spends_only_the_remaining_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work before the lock reduces the lock wait instead of extending it.

    Installation budgets its lock wait from the seconds left, not from the
    configured deadline, so a slow earlier phase cannot make the whole call
    outlive the budget.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    holder = _LockHolder(layout)
    holder.start()
    try:
        assert holder.held.wait(10), "the lock holder never acquired the lock"
        real_open_lock_file = install_state._open_lock_file

        def slow_open_lock_file(path: Path, budget: InstallBudget | None) -> int:
            """Spend part of the budget before the lock wait begins."""
            time.sleep(1.5)
            return real_open_lock_file(path, budget)

        monkeypatch.setattr(install_state, "_open_lock_file", slow_open_lock_file)
        options = dc.replace(
            install_options(case),
            deadline_seconds=2.0,
            lock_timeout_seconds=30.0,
        )
        started = time.monotonic()
        with pytest.raises(ProverToolError, match=r"installation lock"):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        holder.release.set()
        holder.join(10)

    assert elapsed < 2.5, (
        f"the lock wait used the configured timeout, not the budget: {elapsed:.1f}s"
    )
    assert load_active(layout) is None, "a refused lock activated a generation"


def test_require_activation_ready_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime-file check does not hold the identity lock on a stalled stat.

    Activation requires four executables to exist before the probe runs. Those
    stats are reads, so a stalled one is abandoned rather than allowed to
    outlive the budget.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=new_generation_name(),
    )
    runtime = runtime_for(layout, case.manifest)
    release = threading.Event()
    stalled = threading.Event()
    real_stat = pathlib.Path.stat

    def stalling_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        """Block the launcher stat like a hung filesystem."""
        if (
            self == runtime.launcher
            and threading.current_thread().name == "snapshot runtime file requirements"
        ):
            stalled.set()
            release.wait(30)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(pathlib.Path, "stat", stalling_stat)
    context = install_module._InstallContext(
        options=install_options(case),
        deadline=Deadline(time.monotonic() + 0.5),
        hooks=install_hooks(case),
    )
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during runtime file requirements",
        ):
            install_module._require_activation_ready(context, runtime)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the runtime-file check never stat the launcher"
    assert elapsed < 10.0, f"the stalled stat outlived the budget: {elapsed:.1f}s"


def test_install_deadline_bounds_a_stalled_asset_directory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asset directory probe is abandoned, not left holding the lock.

    Staging begins by confirming the transport directory exists. That probe is
    a read, so a stalled one is abandoned rather than allowed to outlive the
    budget while the identity lock is held.
    """
    case = make_case(tmp_path)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_is_dir = pathlib.Path.is_dir

    def stalling_is_dir(self: Path) -> bool:
        """Block the asset directory probe like a hung filesystem."""
        is_assets = (
            self == case.assets_dir
            and threading.current_thread() is not threading.main_thread()
        )
        if not is_assets:
            return real_is_dir(self)
        stalled.set()
        release.wait(30)
        try:
            return real_is_dir(self)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "is_dir", stalling_is_dir)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during asset directory check",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the asset directory was never probed"
    assert elapsed < 10.0, f"the stalled probe outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned probe never resumed"


def test_install_deadline_bounds_a_stalled_checksum_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checksum verification is abandoned instead of holding the lock."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    real_verify = install_transport.verify_checksums

    def stalling_verify(
        checksums: Path,
        manifest: SnapshotManifest,
        manifest_sha256: str,
    ) -> None:
        """Block checksum verification like a hung filesystem."""
        if threading.current_thread().name != "snapshot checksum verification":
            real_verify(checksums, manifest, manifest_sha256)
            return
        stalled.set()
        release.wait(30)
        try:
            real_verify(checksums, manifest, manifest_sha256)
        finally:
            finished.set()

    monkeypatch.setattr(install_transport, "verify_checksums", stalling_verify)
    options = dc.replace(install_options(case), deadline_seconds=2.0)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during checksum verification",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "checksum verification was never attempted"
    assert elapsed < 10.0, (
        f"the stalled verification outlived the budget: {elapsed:.1f}s"
    )
    assert finished.wait(10), "the abandoned verification never resumed"
    assert not layout.publication_markers(), "a stalled verification published payloads"


def test_install_deadline_bounds_a_stalled_marker_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker listing that precedes publication is bounded as well."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    listing_thread = "snapshot publication marker listing"
    real_listing = SnapshotLayout.publication_markers

    def stalling_listing(self: SnapshotLayout) -> tuple[Path, ...]:
        """Block the publication's marker listing like a hung filesystem."""
        if threading.current_thread().name != listing_thread:
            return real_listing(self)
        stalled.set()
        release.wait(30)
        try:
            return real_listing(self)
        finally:
            finished.set()

    monkeypatch.setattr(SnapshotLayout, "publication_markers", stalling_listing)
    options = dc.replace(install_options(case), deadline_seconds=2.0)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during publication marker listing",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the publication never listed the markers"
    assert elapsed < 10.0, f"the stalled listing outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned listing never resumed"
    assert not layout.publication_markers(), "a stalled listing published a marker"


def test_install_deadline_bounds_a_stalled_activation_sequence_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activation sequence a marker records is read under the budget."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    read_thread = "snapshot activation sequence read"
    real_iterdir = pathlib.Path.iterdir

    def stalling_iterdir(self: Path) -> cabc.Iterator[Path]:
        """Block the publication's activation listing like a hung filesystem."""
        is_records = (
            self == layout.activations_dir
            and threading.current_thread().name == read_thread
        )
        if not is_records:
            return real_iterdir(self)
        stalled.set()
        release.wait(30)
        try:
            return real_iterdir(self)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "iterdir", stalling_iterdir)
    options = dc.replace(install_options(case), deadline_seconds=2.0)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during activation sequence read",
        ):
            install_snapshot(options, install_hooks(case))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the publication never read the activation sequence"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned read never resumed"
    assert not layout.publication_markers(), "a stalled read published a marker"


def test_write_active_bounds_a_stalled_activation_directory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording an activation does not hang on a stalled directory probe."""
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    probe_thread = "snapshot activation directory check"
    real_is_dir = pathlib.Path.is_dir

    def stalling_is_dir(self: Path) -> bool:
        """Block the activation directory probe like a hung filesystem."""
        is_records = (
            self == layout.activations_dir
            and threading.current_thread().name == probe_thread
        )
        if not is_records:
            return real_is_dir(self)
        stalled.set()
        release.wait(30)
        try:
            return real_is_dir(self)
        finally:
            finished.set()

    monkeypatch.setattr(pathlib.Path, "is_dir", stalling_is_dir)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during activation directory check",
        ):
            activation.write_active(
                layout,
                new_generation_name(),
                budget=Deadline(time.monotonic() + 0.5),
            )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the activation directory was never probed"
    assert elapsed < 10.0, f"the stalled probe outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned probe never resumed"


def test_check_deadline_bounds_a_stalled_publication_state_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check` bounds the read that decides whether a marker is still pending."""
    case = make_case(tmp_path)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    phase_thread = "snapshot publication state read"
    real_pending = install_module.publication_pending

    def stalling_pending(layout: SnapshotLayout) -> bool:
        """Block the publication-state read like a hung filesystem."""
        if threading.current_thread().name != phase_thread:
            return real_pending(layout)
        stalled.set()
        release.wait(30)
        try:
            return real_pending(layout)
        finally:
            finished.set()

    monkeypatch.setattr(install_module, "publication_pending", stalling_pending)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during publication state read",
        ):
            check_snapshot(options)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the publication state was never read"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned read never resumed"


def test_check_deadline_bounds_a_stalled_active_pointer_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check` bounds the read that resolves the active pointer."""
    case = make_case(tmp_path)
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    phase_thread = "snapshot active pointer read"
    real_load = install_module.load_active

    def stalling_load(layout: SnapshotLayout) -> activation.ActivePointer | None:
        """Block the pointer read like a hung filesystem."""
        if threading.current_thread().name != phase_thread:
            return real_load(layout)
        stalled.set()
        release.wait(30)
        try:
            return real_load(layout)
        finally:
            finished.set()

    monkeypatch.setattr(install_module, "load_active", stalling_load)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during active pointer read",
        ):
            check_snapshot(options)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the active pointer was never read"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned read never resumed"


def test_check_deadline_bounds_a_stalled_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check` bounds the read that loads the active generation's manifest."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    release = threading.Event()
    finished = threading.Event()
    stalled = threading.Event()
    phase_thread = "snapshot manifest read"
    real_manifest = install_module.active_manifest

    def stalling_manifest(
        pin: SnapshotPin,
        layout: SnapshotLayout,
    ) -> SnapshotManifest:
        """Block the manifest read like a hung filesystem."""
        if threading.current_thread().name != phase_thread:
            return real_manifest(pin, layout)
        stalled.set()
        release.wait(30)
        try:
            return real_manifest(pin, layout)
        finally:
            finished.set()

    monkeypatch.setattr(install_module, "active_manifest", stalling_manifest)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            ProverToolError,
            match=r"deadline expired during manifest read",
        ):
            check_snapshot(options)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert stalled.is_set(), "the manifest was never read"
    assert elapsed < 10.0, f"the stalled read outlived the budget: {elapsed:.1f}s"
    assert finished.wait(10), "the abandoned read never resumed"


def test_a_marker_is_pending_until_a_completion_supersedes_its_base(
    tmp_path: Path,
) -> None:
    """Only a completion above a marker's base retires it.

    A marker names the highest activation sequence its publication observed.
    An installation links its record above that base and then records
    completion, so the watermark exceeds the base only when an installation
    finished after the publication began. A marker the watermark merely equals,
    and one that names no base at all, both stay pending.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    pointer = load_active(layout)
    assert pointer is not None, "the install activated no generation"
    base = activation.highest_sequence(layout)
    assert base == _completion(layout), (
        "the completed install did not leave its own record as the highest"
    )
    marker = layout.publication_marker("completion-test")

    marker.write_bytes(publication.render_marker(base))
    assert install_state.publication_pending(layout), (
        "a marker at the last completed record was treated as superseded"
    )

    # A later installation links its record above the marker's base, so its
    # completion supersedes the marker.
    activation.write_active(layout, pointer.generation)
    marker.write_bytes(publication.render_marker(base))
    assert not install_state.publication_pending(layout), (
        "a marker below the last completion was treated as pending"
    )
    check_snapshot(install_options(case), hooks)

    superseding = activation.highest_sequence(layout)
    marker.write_bytes(publication.render_marker(superseding))
    assert install_state.publication_pending(layout), (
        "a marker at the last completed record was not treated as pending"
    )
    marker.write_bytes(b"publishing\n")
    assert install_state.publication_pending(layout), (
        "a marker without a readable base was not treated as pending"
    )
    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), hooks)


class _ReuseValidationFailure:
    """Fail the first reuse validation so the next attempt publishes.

    The cache itself stays valid, so the install after the failed publication
    reuses the generation the first install activated.
    """

    def __init__(self) -> None:
        """Count reuse validations from zero."""
        self.calls = 0

    def __call__(
        self,
        options: InstallOptions,
        manifest: SnapshotManifest,
        layout: SnapshotLayout,
        probe: ProbeRunner,
        remaining_budget: cabc.Callable[[], float] | None = None,
        *,
        checkpoint: cabc.Callable[[], None] | None = None,
    ) -> SnapshotIdentity:
        """Fail once, then validate exactly as the real implementation does."""
        self.calls += 1
        if self.calls == 1:
            msg = "injected reuse validation failure"
            raise ProverToolError(msg)
        return validate_generation(
            options,
            manifest,
            layout,
            probe,
            remaining_budget,
            checkpoint=checkpoint,
        )


class _MarkerWriteStall:
    """Hold a publication marker's durable write, then report its publication.

    The stall lives in `fsync`, inside the supervised worker that writes the
    marker, so the installation deadline expires exactly as it would on a hung
    filesystem and the pending marker is not yet visible.
    """

    def __init__(self) -> None:
        """Prepare the release, stall and publication events."""
        self.release = threading.Event()
        self.stalled = threading.Event()
        self.finished = threading.Event()
        self._real_fsync = os.fsync
        self._real_replace = pathlib.Path.replace

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the filesystem calls the marker write performs."""

        def stalling_fsync(descriptor: int) -> None:
            """Hold the marker's durable write like a hung filesystem."""
            if threading.current_thread().name == "snapshot publication marker":
                self.stalled.set()
                self.release.wait(30)
            self._real_fsync(descriptor)

        def marking_replace(source: Path, target: Path) -> None:
            """Record the moment the abandoned worker publishes its marker."""
            self._real_replace(source, target)
            if target.name.startswith(PUBLICATION_MARKER_PREFIX):
                self.finished.set()

        monkeypatch.setattr(install_generation.os, "fsync", stalling_fsync)
        monkeypatch.setattr(pathlib.Path, "replace", marking_replace)


def test_a_late_marker_cannot_make_a_reused_cache_unfinished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker published after a later reuse cannot make the cache refused.

    A repair publication is abandoned while writing its marker, so no marker
    is visible when a later install reuses and revalidates the generation.
    When the abandoned worker finally publishes its marker, the record that
    reuse wrote outranks it, and readers must still accept the cache.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    monkeypatch.setattr(
        install_module,
        "validate_generation",
        _ReuseValidationFailure(),
    )
    stall = _MarkerWriteStall()
    stall.install(monkeypatch)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    with pytest.raises(ProverToolError, match=r"deadline expired during"):
        install_snapshot(options, hooks)
    assert stall.stalled.is_set(), "the publication marker write was never attempted"
    assert not layout.publication_markers(), (
        "the abandoned write published its marker before the deadline"
    )

    reused = install_snapshot(install_options(case), hooks)
    assert reused.generation.name == first.generation.name, (
        "the later install did not reuse the validated generation"
    )
    check_snapshot(install_options(case), hooks)

    try:
        stall.release.set()
        assert stall.finished.wait(10), "the abandoned marker write never resumed"
    finally:
        stall.release.set()

    markers = layout.publication_markers()
    assert len(markers) == 1, (
        f"the abandoned write published {len(markers)} markers instead of one"
    )
    check_snapshot(install_options(case), hooks)

    install_snapshot(install_options(case), hooks)
    assert not layout.publication_markers(), (
        "a later reuse left the superseded marker behind"
    )
    check_snapshot(install_options(case), hooks)


def test_a_late_activation_record_cannot_hide_an_interrupted_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated activation does not retire an unfinished repair's marker.

    The repair records the completion watermark it observed and is abandoned
    while writing its marker. Another worker's activation lands after that
    point, reissuing the generation that is already active; because it records
    no completion, the interrupted repair's marker must still make readers
    refuse the cache.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    monkeypatch.setattr(
        install_module,
        "validate_generation",
        _ReuseValidationFailure(),
    )
    stall = _MarkerWriteStall()
    stall.install(monkeypatch)
    options = dc.replace(install_options(case), deadline_seconds=0.5)
    with pytest.raises(ProverToolError, match=r"deadline expired during"):
        install_snapshot(options, hooks)
    assert stall.stalled.is_set(), "the publication marker write was never attempted"

    # Another worker's activation lands while the repair's marker write is
    # stalled, the way an abandoned reuse that got past its link would leave
    # one: a new record reissuing the generation that is already active.
    sequence = max(int(path.stem) for path in layout.activations_dir.glob("*.json"))
    late_record = layout.activations_dir / f"{sequence + 1:020d}.json"
    late_record.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "generation": first.generation.name,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )

    try:
        stall.release.set()
        assert stall.finished.wait(10), "the abandoned marker write never resumed"
    finally:
        stall.release.set()

    assert layout.publication_markers(), "the abandoned repair published no marker"
    with pytest.raises(ProverToolError, match=r"publication did not finish"):
        check_snapshot(install_options(case), hooks)


def test_repair_recovers_beyond_a_corrupt_activation_record(
    tmp_path: Path,
) -> None:
    """A corrupt newest record is replaced instead of blocking every repair.

    Publication does not read the activation record, so the ordering a repair
    needs comes from the completion watermark, which the corrupt document
    cannot affect.
    """
    case = make_case(tmp_path)
    hooks = install_hooks(case)
    first = install_snapshot(install_options(case), hooks)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    newest = max(layout.activations_dir.glob("*.json"))
    newest.write_bytes(b'{"schema_version": 1, "generation": ')

    with pytest.raises(ProverToolError):
        check_snapshot(install_options(case), hooks)

    repaired = install_snapshot(install_options(case), hooks)
    assert repaired.generation.name != first.generation.name, (
        "the repair reactivated the generation whose record was corrupt"
    )
    pointer = load_active(layout)
    assert pointer is not None, "the repair activated no generation"
    assert pointer.generation == repaired.generation.name, (
        f"the repair left {pointer.generation} active instead of "
        f"{repaired.generation.name}"
    )
    check_snapshot(install_options(case), hooks)


class _LockHolder(threading.Thread):
    """Hold the identity lock from a second open file description."""

    def __init__(self, layout: SnapshotLayout) -> None:
        """Prepare the holder, its release event and its readiness event."""
        super().__init__(name="identity lock holder", daemon=True)
        self._layout = layout
        self.held = threading.Event()
        self.release = threading.Event()

    def run(self) -> None:
        """Acquire the lock, hold it until released, then unlock it."""
        path = self._layout.identity_dir / "install.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.held.set()
            self.release.wait(30)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
