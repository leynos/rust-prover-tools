"""Installation fixtures shared by the snapshot contract tests.

The helpers build one complete local snapshot and the injected hooks that model
a successful setup without external tools. Keeping them here lets both the
installation and execution test modules use the same doubles without importing
one test module from another.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.kani_snapshot.archive import extract_archive
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install_model import InstallHooks, InstallOptions
from rust_prover_tools.kani_snapshot.install_transport import bundle_policy
from rust_prover_tools.kani_snapshot.receipt_model import (
    ObservedToolchain,
    SetupEvidence,
)
from tests.kani_snapshot_bundles import (
    ATTESTATION_NAME,
    MANIFEST_NAME,
    build_local_snapshot,
    publish_manifest,
    sha256_hex,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "SnapshotCase",
    "active_generation",
    "install_hooks",
    "install_options",
    "make_case",
    "rewrite_manifest_asset",
)


@dc.dataclass(slots=True)
class SnapshotCase:
    """Fixture snapshot assets and the records that describe them."""

    pin: SnapshotPin
    manifest: SnapshotManifest
    assets_dir: Path
    cache_home: Path


def make_case(tmp_path: Path) -> SnapshotCase:
    """Create one complete set of local snapshot assets."""
    snapshot = build_local_snapshot(tmp_path / "assets")
    return SnapshotCase(
        pin=snapshot.pin,
        manifest=snapshot.manifest,
        assets_dir=snapshot.assets_dir,
        cache_home=tmp_path,
    )


def install_hooks(
    case: SnapshotCase,
    *,
    setup_calls: list[Path] | None = None,
    probe: cabc.Callable[[SnapshotRuntime], SetupEvidence] | None = None,
    attestation: cabc.Callable[[object, Path, Path], object] | None = None,
) -> InstallHooks:
    """Return fakes that model a successful setup without external tools."""
    calls = setup_calls if setup_calls is not None else []

    def fake_setup(
        runtime: SnapshotRuntime,
        bundle: Path,
        _timeout_seconds: float | None,
    ) -> None:
        """Create the files a successful setup would install."""
        calls.append(bundle)
        runtime.layout.kani_home.mkdir(parents=True, exist_ok=True)
        kani = runtime.kani_dir
        # Real setup consumes the verified bundle, so the double extracts the
        # same archive the production installer hands it.
        extract_archive(
            bundle,
            kani,
            policy=bundle_policy(runtime.backend_version),
        )
        (kani / "pyroot").mkdir()
        target = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "rustc").write_text("rustc", encoding="utf-8")
        (target / "bin" / "rustc").chmod(0o755)
        (kani / "toolchain").symlink_to(target, target_is_directory=True)

    def fake_probe(
        runtime: SnapshotRuntime,
        _timeout_seconds: float | None,
    ) -> SetupEvidence:
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
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        """Accept the expected manifest and bundle, or delegate to the caller."""
        del timeout_seconds
        if attestation is not None:
            return attestation(policy, artifact, bundle)
        if artifact.name != MANIFEST_NAME:
            msg = f"unexpected manifest asset: {artifact.name}"
            raise ValueError(msg)
        if bundle.name != ATTESTATION_NAME:
            msg = f"unexpected attestation asset: {bundle.name}"
            raise ValueError(msg)
        return ({"verificationResult": {}},)

    return InstallHooks(
        setup=fake_setup,
        probe=fake_probe,
        verify_attestation=fake_attestation,
    )


def install_options(
    case: SnapshotCase,
    *,
    assets_dir: Path | None = None,
) -> InstallOptions:
    """Return installation options for the fixture."""
    return InstallOptions(
        pin=case.pin,
        assets_dir=case.assets_dir if assets_dir is None else assets_dir,
        cache_home=case.cache_home,
    )


def active_generation(case: SnapshotCase) -> str:
    """Return the generation name recorded by the active pointer."""
    pointer = (
        snapshot_layout(case.pin, cache_home=case.cache_home).identity_dir
        / "active.json"
    )
    document = json.loads(pointer.read_text(encoding="utf-8"))
    return typ.cast("str", document["generation"])


def rewrite_manifest_asset(case: SnapshotCase, name: str, data: bytes) -> None:
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
