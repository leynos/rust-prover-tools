"""Installation fixtures shared by the snapshot contract tests.

The helpers build one complete local snapshot and the injected hooks that model
a successful setup without external tools. Keeping them here lets both the
installation and execution test modules use the same doubles without importing
one test module from another.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_snapshot.archive import extract_archive
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install_model import InstallHooks, InstallOptions
from rust_prover_tools.kani_snapshot.install_state import load_active
from rust_prover_tools.kani_snapshot.install_transport import bundle_policy
from rust_prover_tools.kani_snapshot.receipt_model import (
    ObservedToolchain,
    SetupEvidence,
)
from tests.kani_snapshot_bundles import (
    ATTESTATION_NAME,
    LIBRARY_BYTES,
    LIBRARY_PATH,
    MANIFEST_NAME,
    RUSTC_BYTES,
    RUSTUP_COMPONENTS,
    build_local_snapshot,
    publish_manifest,
    sha256_hex,
    write_file,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest

    from rust_prover_tools.kani_snapshot.attestation import AttestationRequest
    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "PYTHON_ROOT_FILE",
    "SnapshotCase",
    "active_generation",
    "events_from",
    "install_hooks",
    "install_options",
    "make_case",
    "rewrite_manifest_asset",
)

#: One setup-created Python payload, recorded with the root's content contract.
PYTHON_ROOT_FILE: typ.Final[str] = "site/kani/__init__.py"
_PYTHON_ROOT_BYTES: typ.Final[bytes] = b'"""Fixture Kani Python payload."""\n'


def events_from(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured events captured by `caplog`, in order.

    Parameters
    ----------
    caplog : pytest.LogCaptureFixture
        Capture fixture for the test asserting on the events.

    Returns
    -------
    list[dict[str, object]]
        One `event_data` mapping per structured record.
    """
    return [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]


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
        write_file(kani / "pyroot" / PYTHON_ROOT_FILE, _PYTHON_ROOT_BYTES)
        # The toolchain bytes are exactly what the manifest digests describe,
        # so the double mirrors real setup rather than merely its shape.
        target = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
        write_file(target / "bin" / "rustc", RUSTC_BYTES, 0o755)
        write_file(target / "lib" / "rustlib" / "components", RUSTUP_COMPONENTS)
        write_file(target / LIBRARY_PATH, LIBRARY_BYTES)
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

    return InstallHooks(
        setup=fake_setup,
        probe=fake_probe,
        verify_attestation=_attestation_verifier(case, attestation),
    )


def _attestation_verifier(
    case: SnapshotCase,
    attestation: cabc.Callable[[object, Path, Path], object] | None,
) -> cabc.Callable[..., object]:
    """Build a verifier that checks the request before accepting it."""

    def fake_attestation(
        request: AttestationRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        """Accept the expected manifest and bundle, or delegate to the caller."""
        del timeout_seconds
        # The installer must verify against the manifest's own run, so a double
        # that received anything else models a real installation defect.
        if request.workflow != case.manifest.workflow:
            msg = f"unexpected workflow identity: {request.workflow!r}"
            raise ValueError(msg)
        if attestation is not None:
            return attestation(request.policy, request.artifact, request.bundle)
        if request.artifact.name != MANIFEST_NAME:
            msg = f"unexpected manifest asset: {request.artifact.name}"
            raise ValueError(msg)
        if request.bundle.name != ATTESTATION_NAME:
            msg = f"unexpected attestation asset: {request.bundle.name}"
            raise ValueError(msg)
        return ({"verificationResult": {}},)

    return fake_attestation


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
    """Return the generation named by the highest activation record."""
    pointer = load_active(snapshot_layout(case.pin, cache_home=case.cache_home))
    if pointer is None:
        msg = "the fixture installed no active generation"
        raise AssertionError(msg)
    return pointer.generation


def rewrite_manifest_asset(case: SnapshotCase, name: str, data: bytes) -> None:
    """Rewrite one asset plus the manifest, checksums and pin naming it."""
    write_file(case.assets_dir / name, data)
    records = tuple(
        dc.replace(asset, size=len(data), sha256=sha256_hex(data))
        if asset.name == name
        else asset
        for asset in case.manifest.assets
    )
    manifest = dc.replace(case.manifest, assets=records)
    case.pin = publish_manifest(case.assets_dir, manifest, base_pin=case.pin)
    case.manifest = manifest
