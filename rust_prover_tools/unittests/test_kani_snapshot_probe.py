"""Contract tests for the compiler identity probe's admission route."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools import commands
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import install_defaults
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install_defaults import probe_generation
from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime
from tests.kani_snapshot_bundles import KANI_COMPILER, RUSTC_VV, TOOLCHAIN, VERSION
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    from pathlib import Path


def _runtime(tmp_path: Path) -> SnapshotRuntime:
    """Return a runtime with the toolchain link a probe expects."""
    runtime = SnapshotRuntime(
        layout=snapshot_layout(make_pin(), cache_home=tmp_path, generation="gen"),
        toolchain=TOOLCHAIN,
        backend_version=VERSION,
    )
    target = runtime.layout.rustup_home / "toolchains" / TOOLCHAIN
    target.mkdir(parents=True)
    runtime.kani_dir.mkdir(parents=True)
    (runtime.kani_dir / "toolchain").symlink_to(target, target_is_directory=True)
    return runtime


def _record_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, ...]]:
    """Replace command execution and record every argument vector."""
    seen: list[tuple[str, ...]] = []

    def fake_run_checked(spec: commands.CommandSpec) -> commands.CommandResult:
        """Return the identity matching the probed executable."""
        seen.append(spec.argv)
        program = spec.argv[-2]
        stdout = KANI_COMPILER if program.endswith("kani-compiler") else RUSTC_VV
        return commands.CommandResult(stdout=stdout, stderr="", exit_code=0)

    monkeypatch.setattr(install_defaults, "run_checked", fake_run_checked)
    return seen


def test_probe_runs_through_the_configured_admission_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every probed compiler starts with the configured wrapper."""
    wrapper = "/usr/bin/build-limits-rustc"
    monkeypatch.setenv("RUSTC_WRAPPER", wrapper)
    seen = _record_commands(monkeypatch)

    runtime = _runtime(tmp_path)

    evidence = probe_generation(runtime)

    assert evidence.observed.rustc_vv == RUSTC_VV
    assert [argv[0] for argv in seen] == [wrapper, wrapper]
    assert [argv[1] for argv in seen] == [
        str(runtime.rustc),
        str(runtime.compiler),
    ]


def test_probe_refuses_a_malformed_admission_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable route fails before either executable is invoked."""
    monkeypatch.setenv("RUSTC_WRAPPER", "")
    seen = _record_commands(monkeypatch)

    with pytest.raises(ProverToolError, match=r"process tool selection"):
        probe_generation(_runtime(tmp_path))
    assert seen == [], "a malformed admission route still ran a probe"
