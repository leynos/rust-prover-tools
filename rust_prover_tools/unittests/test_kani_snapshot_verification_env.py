"""Unit tests for the child environment of a snapshot proof."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime
from rust_prover_tools.kani_snapshot.verification_env import proof_environment
from tests.kani_snapshot_bundles import TOOLCHAIN, VERSION
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    from pathlib import Path


def _runtime(tmp_path: Path) -> SnapshotRuntime:
    """Return a runtime rooted at a temporary generation."""
    return SnapshotRuntime(
        layout=snapshot_layout(make_pin(), cache_home=tmp_path, generation="gen"),
        toolchain=TOOLCHAIN,
        backend_version=VERSION,
    )


def _base(tmp_path: Path, **values: str) -> dict[str, str]:
    """Return a minimal inherited environment with a usable HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": "/usr/bin", **values}


def _cargo_config(cwd: Path, body: str) -> None:
    """Write one repository Cargo configuration file."""
    directory = cwd / ".cargo"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(body, encoding="utf-8")


def test_proof_environment_pins_a_discovered_backend_to_llvm(
    tmp_path: Path,
) -> None:
    """A repository profile backend is shadowed inside the child only."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _cargo_config(cwd, '[profile.dev]\ncodegen-backend = "cranelift"\n')

    env = proof_environment(_runtime(tmp_path), _base(tmp_path), cwd=cwd)

    assert env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm"


def test_proof_environment_normalizes_both_flag_spellings(tmp_path: Path) -> None:
    """Inherited coverage atoms and development options never reach Kani."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    base = _base(
        tmp_path,
        CARGO_ENCODED_RUSTFLAGS="\x1f".join((
            "-Cinstrument-coverage",
            "--cfg=coverage",
            "-Zthreads=8",
        )),
    )

    env = proof_environment(_runtime(tmp_path), base, cwd=cwd)

    assert env["RUSTFLAGS"] == "-D warnings"
    assert env["CARGO_ENCODED_RUSTFLAGS"] == "-D\x1fwarnings"


def test_proof_environment_selects_the_pinned_toolchain_libraries(
    tmp_path: Path,
) -> None:
    """The pinned runtime comes first and host toolchains are dropped."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    runtime = _runtime(tmp_path)
    base = _base(
        tmp_path,
        LD_LIBRARY_PATH="/host/toolchains/other/lib:/usr/lib",
    )

    env = proof_environment(runtime, base, cwd=cwd)

    pinned = runtime.layout.rustup_home / "toolchains" / TOOLCHAIN / "lib"
    assert env["LD_LIBRARY_PATH"] == f"{pinned}:/usr/lib"


def test_proof_environment_rejects_a_package_backend_override(
    tmp_path: Path,
) -> None:
    """A package backend is resolved above the environment, so it fails closed."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _cargo_config(
        cwd,
        '[profile.dev.package."*"]\ncodegen-backend = "cranelift"\n',
    )

    with pytest.raises(ProverToolError, match=r"package"):
        proof_environment(_runtime(tmp_path), _base(tmp_path), cwd=cwd)


def test_proof_environment_rejects_an_environment_backend_selection(
    tmp_path: Path,
) -> None:
    """An inherited profile backend cannot be proven safe and is refused."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    base = _base(tmp_path, CARGO_PROFILE_DEV_CODEGEN_BACKEND="cranelift")

    with pytest.raises(ProverToolError, match=r"cannot isolate"):
        proof_environment(_runtime(tmp_path), base, cwd=cwd)


def test_proof_environment_keeps_admission_wrappers(tmp_path: Path) -> None:
    """Configured admission routes are retained rather than overridden.

    The environment is an overlay, so a retained route is simply absent from
    the result and stays inherited by the child.
    """
    cwd = tmp_path / "repo"
    cwd.mkdir()
    base = _base(
        tmp_path,
        RUSTC_WRAPPER="/usr/bin/build-limits-rustc",
        RUSTDOC="/usr/bin/build-limits-rustdoc",
    )

    env = proof_environment(_runtime(tmp_path), base, cwd=cwd)

    assert "RUSTC_WRAPPER" not in env, "the overlay replaced an admission wrapper"
    assert "RUSTDOC" not in env, "the overlay replaced an admission doc route"


def test_proof_environment_rejects_a_malformed_admission_wrapper(
    tmp_path: Path,
) -> None:
    """An unusable compiler route is reported instead of silently ignored."""
    cwd = tmp_path / "repo"
    cwd.mkdir()

    with pytest.raises(ProverToolError, match=r"process tool selection"):
        proof_environment(
            _runtime(tmp_path), _base(tmp_path, RUSTC_WRAPPER=""), cwd=cwd
        )
