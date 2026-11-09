"""Unit tests for verifying extracted toolchain bytes before execution."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime
from rust_prover_tools.kani_snapshot.toolchain_binding import verify_toolchain
from tests.kani_snapshot_bundles import (
    LIBRARY_BYTES,
    LIBRARY_PATH,
    RUSTC_BYTES,
    RUSTUP_COMPONENTS,
    TOOLCHAIN,
    VERSION,
    manifest_for,
    payloads,
    sha256_hex,
)
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotManifest


def _runtime(tmp_path: Path) -> SnapshotRuntime:
    """Return a runtime whose toolchain root holds the fixture bytes."""
    runtime = SnapshotRuntime(
        layout=snapshot_layout(make_pin(), cache_home=tmp_path, generation="gen"),
        toolchain=TOOLCHAIN,
        backend_version=VERSION,
    )
    root = _toolchain_root(runtime)
    _write(root / "bin" / "rustc", RUSTC_BYTES, mode=0o755)
    _write(root / "lib" / "rustlib" / "components", RUSTUP_COMPONENTS)
    _write(root / LIBRARY_PATH, LIBRARY_BYTES)
    return runtime


def _toolchain_root(runtime: SnapshotRuntime) -> Path:
    """Return the runtime's toolchain root."""
    return runtime.layout.rustup_home / "toolchains" / runtime.toolchain


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write one fixture file, creating its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _manifest(**toolchain_changes: object) -> SnapshotManifest:
    """Return the fixture manifest with selected toolchain fields replaced."""
    manifest = manifest_for(payloads())
    return dc.replace(
        manifest,
        toolchain=dc.replace(manifest.toolchain, **toolchain_changes),
    )


def test_verify_toolchain_accepts_the_declared_bytes(tmp_path: Path) -> None:
    """A toolchain installed exactly as recorded is accepted."""
    verify_toolchain(_runtime(tmp_path), _manifest())


def test_verify_toolchain_rejects_a_modified_compiler(tmp_path: Path) -> None:
    """Compiler bytes that differ from the manifest are refused."""
    runtime = _runtime(tmp_path)
    compiler = _toolchain_root(runtime) / "bin" / "rustc"
    compiler.write_bytes(RUSTC_BYTES.replace(b"printf", b"exec  "))

    with pytest.raises(ProverToolError, match=r"compiler.*does not match"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_modified_library(tmp_path: Path) -> None:
    """An extracted library that differs from the manifest is refused."""
    runtime = _runtime(tmp_path)
    (_toolchain_root(runtime) / LIBRARY_PATH).write_bytes(b"tampered library\n")

    with pytest.raises(ProverToolError, match=r"library.*does not match"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_modified_component_record(
    tmp_path: Path,
) -> None:
    """The installed rustup component list must match its recorded digest."""
    runtime = _runtime(tmp_path)
    (_toolchain_root(runtime) / "lib" / "rustlib" / "components").write_bytes(
        RUSTUP_COMPONENTS + b"rust-docs\n"
    )

    with pytest.raises(ProverToolError, match=r"component records.*does not match"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_missing_compiler(tmp_path: Path) -> None:
    """A missing compiler is reported rather than treated as absent-but-fine."""
    runtime = _runtime(tmp_path)
    (_toolchain_root(runtime) / "bin" / "rustc").unlink()

    with pytest.raises(ProverToolError, match=r"compiler is missing"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_missing_library(tmp_path: Path) -> None:
    """A declared library that setup did not install is refused."""
    runtime = _runtime(tmp_path)
    (_toolchain_root(runtime) / LIBRARY_PATH).unlink()

    with pytest.raises(ProverToolError, match=r"library.*is missing"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_an_unrecorded_shared_library(
    tmp_path: Path,
) -> None:
    """A shared object the pinned library path exposes must be recorded."""
    runtime = _runtime(tmp_path)
    _write(_toolchain_root(runtime) / "lib" / "libextra.so", b"not recorded\n")

    with pytest.raises(ProverToolError, match=r"does not record"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_library_link_that_escapes(
    tmp_path: Path,
) -> None:
    """A recorded-path scan must also reject a link out of the toolchain."""
    runtime = _runtime(tmp_path)
    outside = tmp_path / "outside-library.so"
    outside.write_bytes(b"outside\n")
    (_toolchain_root(runtime) / "lib" / "liboutside.so").symlink_to(outside)

    with pytest.raises(ProverToolError, match=r"escapes its toolchain"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_a_toolchain_link_out_of_the_rustup_home(
    tmp_path: Path,
) -> None:
    """The identifier must resolve inside the generation's private rustup home."""
    runtime = SnapshotRuntime(
        layout=snapshot_layout(make_pin(), cache_home=tmp_path, generation="gen"),
        toolchain=TOOLCHAIN,
        backend_version=VERSION,
    )
    outside = tmp_path / "outside-toolchain"
    outside.mkdir()
    root = _toolchain_root(runtime)
    root.parent.mkdir(parents=True)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProverToolError, match=r"escapes the private rustup home"):
        verify_toolchain(runtime, _manifest())


def test_verify_toolchain_rejects_an_identifier_that_is_a_path(
    tmp_path: Path,
) -> None:
    """A revalidated identifier cannot leave the private rustup home."""
    runtime = dc.replace(_runtime(tmp_path), toolchain="../escape")

    with pytest.raises(ProverToolError, match=r"one plain name"):
        verify_toolchain(runtime, _manifest())


def test_manifest_declares_the_fixture_digests() -> None:
    """The fixture manifest describes the bytes the fixture setup installs."""
    toolchain = manifest_for(payloads()).toolchain
    assert toolchain.rustc_sha256 == sha256_hex(RUSTC_BYTES)
    assert toolchain.rustup_components_sha256 == sha256_hex(RUSTUP_COMPONENTS)
    assert [(library.path, library.sha256) for library in toolchain.libraries] == [
        (LIBRARY_PATH, sha256_hex(LIBRARY_BYTES))
    ]
