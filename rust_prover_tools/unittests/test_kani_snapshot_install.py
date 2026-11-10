"""Contract tests for snapshot installation, activation and cache reuse."""

from __future__ import annotations

import dataclasses as dc
import io
import shutil
import tarfile
import threading
import time
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import (
    activation,
    install_copy,
    install_generation,
    install_state,
    install_transport,
)
from rust_prover_tools.kani_snapshot import (
    install as install_module,
)
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    InstallOptions,
    check_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.install_state import (
    load_active,
    validate_generation,
)
from rust_prover_tools.kani_snapshot.receipt import ObservedToolchain, SetupEvidence
from rust_prover_tools.kani_snapshot.snapshot_layout import new_generation_name
from tests.kani_snapshot_bundles import (
    CHECKSUMS_NAME,
    LIBRARY_PATH,
    MANIFEST_NAME,
    RUSTC_BYTES,
    payloads,
    publish_manifest,
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

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

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
    pointer = snapshot_layout(case.pin, cache_home=tmp_path).active_path
    assert not pointer.exists(), "a mismatched probe activated a generation"


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
    """An expiry before the rename leaves no new pointer and no temporary file.

    Installation checks the budget after the pointer document is durable and
    immediately before the rename, so the only visible states are the previous
    pointer and the complete new one.
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

    assert not layout.active_path.exists(), "an expired budget published a pointer"
    leftovers = list(layout.identity_dir.glob(".active-*"))
    assert not leftovers, f"an expired budget left temporary pointers: {leftovers}"


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

    The rename has already made the generation visible, so reporting a
    publication failure would be untrue. The error names the generation and
    the pointer is left naming it.
    """
    case = make_case(tmp_path)
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)
    install_state.ensure_private_directory(layout.identity_dir)
    generation = new_generation_name()
    real_flush = activation.fsync_directory

    def failing_flush(path: Path) -> None:
        """Fail the identity-directory flush, as a broken filesystem would."""
        if path == layout.identity_dir:
            msg = "input/output error"
            raise OSError(msg)
        real_flush(path)

    monkeypatch.setattr(activation, "fsync_directory", failing_flush)

    with pytest.raises(ProverToolError, match=r"active but its activation"):
        install_state.write_active(layout, generation)

    pointer = load_active(layout)
    assert pointer is not None, "the generation is not visible after the failure"
    assert pointer.generation == generation, "the pointer names another generation"
    leftovers = list(layout.identity_dir.glob(".active-*"))
    assert not leftovers, f"a failed flush left temporary pointers: {leftovers}"


def test_activation_flush_timeout_is_reported_as_durability(
    tmp_path: Path,
) -> None:
    """An expiry during the post-rename flush does not claim publication failed."""
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
