"""Contract tests for snapshot installation, activation and cache reuse."""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    InstallHooks,
    InstallOptions,
    check_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.receipt import ObservedToolchain, SetupEvidence
from tests.kani_snapshot_bundles import (
    ATTESTATION_NAME,
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    build_local_snapshot,
    publish_manifest,
    sha256_hex,
    tar_bytes,
    write_file,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest, SnapshotPin


@dc.dataclass(slots=True)
class _Case:
    """Fixture snapshot assets and the records that describe them."""

    pin: SnapshotPin
    manifest: SnapshotManifest
    assets_dir: Path
    cache_home: Path


def _make_case(tmp_path: Path) -> _Case:
    """Create one complete set of local snapshot assets."""
    snapshot = build_local_snapshot(tmp_path / "assets")
    return _Case(
        pin=snapshot.pin,
        manifest=snapshot.manifest,
        assets_dir=snapshot.assets_dir,
        cache_home=tmp_path,
    )


def _hooks(
    case: _Case,
    *,
    setup_calls: list[Path] | None = None,
    probe: cabc.Callable[[SnapshotRuntime], SetupEvidence] | None = None,
    attestation: cabc.Callable[[object, Path, Path], object] | None = None,
) -> InstallHooks:
    """Return fakes that model a successful setup without external tools."""
    calls = setup_calls if setup_calls is not None else []

    def fake_setup(runtime: SnapshotRuntime, bundle: Path) -> None:
        """Create the files a successful setup would install."""
        calls.append(bundle)
        runtime.layout.kani_home.mkdir(parents=True, exist_ok=True)
        kani = runtime.kani_dir
        bin_dir = kani / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "kani-driver").write_bytes(b"driver")
        (bin_dir / "kani-compiler").write_bytes(b"compiler")
        (bin_dir / "kani-driver").chmod(0o755)
        (bin_dir / "kani-compiler").chmod(0o755)
        (kani / "pyroot").mkdir()
        target = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "rustc").write_text("rustc", encoding="utf-8")
        (target / "bin" / "rustc").chmod(0o755)
        (kani / "toolchain").symlink_to(target, target_is_directory=True)

    def fake_probe(runtime: SnapshotRuntime) -> SetupEvidence:
        """Return the caller's evidence or a manifest-matching observation."""
        if probe is not None:
            return probe(runtime)
        return SetupEvidence(
            observed=ObservedToolchain(
                rustc_vv=case.manifest.toolchain.rustc_vv,
                kani_compiler=case.manifest.toolchain.kani_compiler,
            ),
            toolchain_link_target=(
                runtime.layout.rustup_home / "toolchains" / runtime.toolchain
            ),
        )

    def fake_attestation(
        policy: object,
        artifact: Path,
        bundle: Path,
    ) -> object:
        """Accept the expected manifest and bundle, or delegate to the caller."""
        if attestation is not None:
            return attestation(policy, artifact, bundle)
        assert artifact.name == MANIFEST_NAME
        assert bundle.name == ATTESTATION_NAME
        return ({"verificationResult": {}},)

    return InstallHooks(
        setup=fake_setup,
        probe=fake_probe,
        verify_attestation=fake_attestation,
    )


def _options(case: _Case, *, assets_dir: Path | None = None) -> InstallOptions:
    """Return installation options for the fixture."""
    return InstallOptions(
        pin=case.pin,
        assets_dir=case.assets_dir if assets_dir is None else assets_dir,
        cache_home=case.cache_home,
    )


def _active_generation(case: _Case) -> str:
    """Return the generation name recorded by the active pointer."""
    pointer = (
        snapshot_layout(case.pin, cache_home=case.cache_home).identity_dir
        / "active.json"
    )
    document = json.loads(pointer.read_text(encoding="utf-8"))
    return typ.cast("str", document["generation"])


def test_install_activates_a_validated_generation(tmp_path: Path) -> None:
    """A complete local install activates a validated generation."""
    case = _make_case(tmp_path)
    calls: list[Path] = []
    identity = install_snapshot(_options(case), _hooks(case, setup_calls=calls))
    assert identity.generation.name == _active_generation(case)
    assert identity.launcher.exists()
    assert identity.generation.joinpath("receipt.json").is_file()
    assert [path.name for path in calls] == ["backend.tar.gz"]


def test_second_install_reuses_the_active_generation(tmp_path: Path) -> None:
    """A repeated install returns the active generation without setup."""
    case = _make_case(tmp_path)
    calls: list[Path] = []
    hooks = _hooks(case, setup_calls=calls)
    first = install_snapshot(_options(case), hooks)
    second = install_snapshot(_options(case), hooks)
    assert first == second
    assert len(calls) == 1


def test_install_rejects_a_missing_asset(tmp_path: Path) -> None:
    """A missing local payload is reported before setup runs."""
    case = _make_case(tmp_path)
    (case.assets_dir / "backend.tar.gz").unlink()
    calls: list[Path] = []
    with pytest.raises(ProverToolError, match=r"backend\.tar\.gz"):
        install_snapshot(_options(case), _hooks(case, setup_calls=calls))
    assert calls == []


def test_install_rejects_a_manifest_outside_the_reviewed_pin(tmp_path: Path) -> None:
    """A manifest whose bytes differ from the pin is refused."""
    case = _make_case(tmp_path)
    changed = dc.replace(case.pin, manifest_sha256="0" * 64)
    options = InstallOptions(
        pin=changed,
        assets_dir=case.assets_dir,
        cache_home=tmp_path,
    )
    with pytest.raises(ProverToolError, match=r"manifest digest"):
        install_snapshot(options, _hooks(case))


def test_install_rejects_a_failed_attestation(tmp_path: Path) -> None:
    """A rejected attestation stops installation before setup."""
    case = _make_case(tmp_path)
    calls: list[Path] = []

    def reject(_policy: object, _artifact: Path, _bundle: Path) -> object:
        """Refuse the attestation."""
        msg = "attestation rejected"
        raise ProverToolError(msg)

    with pytest.raises(ProverToolError, match=r"attestation rejected"):
        install_snapshot(
            _options(case),
            _hooks(case, setup_calls=calls, attestation=reject),
        )
    assert calls == []


def test_install_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    """A checksum list that disagrees with the manifest is refused."""
    case = _make_case(tmp_path)
    path = case.assets_dir / CHECKSUMS_NAME
    write_file(path, path.read_bytes().replace(b"backend.tar.gz", b"backend.tar.gX"))
    with pytest.raises(ProverToolError, match=r"checksum"):
        install_snapshot(_options(case), _hooks(case))


def test_install_rejects_an_archive_that_disagrees_with_inventory(
    tmp_path: Path,
) -> None:
    """Archive members must match the published inventory."""
    case = _make_case(tmp_path)
    backend = tar_bytes({
        "bin/kani-driver": (b"driver", 0o755),
        "bin/kani-compiler": (b"different", 0o755),
        "share/data.txt": (b"data", 0o644),
    })
    write_file(case.assets_dir / "backend.tar.gz", backend)
    _rewrite_manifest_asset(case, "backend.tar.gz", backend)
    calls: list[Path] = []
    with pytest.raises(ProverToolError, match=r"different size|inventory"):
        install_snapshot(_options(case), _hooks(case, setup_calls=calls))
    assert calls == []


def test_install_rejects_a_mismatched_effective_compiler(tmp_path: Path) -> None:
    """A compiler probe that differs from the manifest fails closed."""
    case = _make_case(tmp_path)
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
            _options(case),
            _hooks(case, setup_calls=calls, probe=wrong),
        )
    assert calls
    pointer = snapshot_layout(case.pin, cache_home=tmp_path).active_path
    assert not pointer.exists()


def test_install_repairs_a_corrupt_active_generation(tmp_path: Path) -> None:
    """A corrupt active generation is replaced without deleting it."""
    case = _make_case(tmp_path)
    calls: list[Path] = []
    hooks = _hooks(case, setup_calls=calls)
    first = install_snapshot(_options(case), hooks)
    receipt = first.generation / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    second = install_snapshot(_options(case), hooks)
    assert second.generation != first.generation
    assert first.generation.is_dir()
    assert first.generation.name != _active_generation(case)
    assert len(calls) == 2


def test_check_reports_the_active_generation(tmp_path: Path) -> None:
    """Checking returns the same validated identity installation did."""
    case = _make_case(tmp_path)
    identity = install_snapshot(_options(case), _hooks(case))
    assert check_snapshot(_options(case), _hooks(case)) == identity


def test_check_rejects_a_missing_active_pointer(tmp_path: Path) -> None:
    """Checking before installation is a clear error."""
    case = _make_case(tmp_path)
    with pytest.raises(ProverToolError, match=r"not installed"):
        check_snapshot(_options(case), _hooks(case))


def test_install_stops_before_setup_when_the_deadline_expired(
    tmp_path: Path,
) -> None:
    """An expired budget prevents setup."""
    case = _make_case(tmp_path)
    calls: list[Path] = []
    options = dc.replace(_options(case), deadline_seconds=0.0)
    with pytest.raises(ProverToolError, match=r"deadline"):
        install_snapshot(options, _hooks(case, setup_calls=calls))
    assert calls == []


def test_install_rejects_an_oversized_manifest(tmp_path: Path) -> None:
    """The manifest transport bound is enforced before parsing."""
    case = _make_case(tmp_path)
    write_file(case.assets_dir / MANIFEST_NAME, b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ProverToolError, match=r"manifest.*limit"):
        install_snapshot(_options(case), _hooks(case))


def _rewrite_manifest_asset(case: _Case, name: str, data: bytes) -> None:
    """Rewrite the manifest, checksums and pin after replacing one asset."""
    records = tuple(
        dc.replace(asset, size=len(data), sha256=sha256_hex(data))
        if asset.name == name
        else asset
        for asset in case.manifest.assets
    )
    manifest = dc.replace(case.manifest, assets=records)
    case.pin = publish_manifest(case.assets_dir, manifest, base_pin=case.pin)
    case.manifest = manifest
