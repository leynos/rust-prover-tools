"""Contract tests for snapshot installation, activation and cache reuse."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    InstallOptions,
    check_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.receipt import ObservedToolchain, SetupEvidence
from tests.kani_snapshot_bundles import (
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    tar_bytes,
    write_file,
)
from tests.kani_snapshot_install_fixtures import (
    active_generation,
    install_hooks,
    install_options,
    make_case,
    rewrite_manifest_asset,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime


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


def test_install_passes_the_remaining_budget_to_setup_and_probe(
    tmp_path: Path,
) -> None:
    """Setup and the probe receive a positive remaining installation budget."""
    case = make_case(tmp_path)
    budgets: list[float | None] = []
    base = install_hooks(case)

    def record_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        timeout_seconds: float | None,
    ) -> None:
        """Run the normal fake setup while recording its budget."""
        budgets.append(timeout_seconds)
        base.setup(runtime, bundle, timeout_seconds)

    def record_probe(
        runtime: SnapshotRuntime,
        timeout_seconds: float | None,
    ) -> SetupEvidence:
        """Run the normal fake probe while recording its budget."""
        budgets.append(timeout_seconds)
        return base.probe(runtime, timeout_seconds)

    hooks = dc.replace(base, setup=record_setup, probe=record_probe)
    install_snapshot(install_options(case), hooks)
    assert len(budgets) == 2, "setup and probe were not both bounded"
    assert all(budget is not None and budget > 0 for budget in budgets), (
        "an external step received no positive remaining budget"
    )
