"""Identity, layout and environment contracts for snapshot execution."""

from __future__ import annotations

import json
import os
import sys
import typing as typ

import pytest
from cuprum.context import UNSET

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.cache_key import identity_key
from rust_prover_tools.kani_snapshot.identity import (
    SnapshotLayout,
    build_identity_report,
    execution_environment,
    identity_directory,
    is_safe_generation_name,
    new_generation_name,
    runtime_for,
    snapshot_layout,
    validate_forwarded_args,
)
from rust_prover_tools.kani_snapshot.models import (
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    ResourceObservation,
    RuntimeIdentity,
    SnapshotManifest,
    SnapshotPin,
    ToolchainIdentity,
    WorkflowIdentity,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_SHA = "a" * 64


def _pin(manifest_sha256: str = _SHA) -> SnapshotPin:
    """Return one validated pin."""
    return SnapshotPin(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-0123456789ab-r1",
        upstream_commit="b" * 40,
        builder_commit="c" * 40,
        target="x86_64-unknown-linux-gnu",
        manifest_sha256=manifest_sha256,
    )


def _manifest() -> SnapshotManifest:
    """Return a manifest with only the fields identity reporting reads."""
    return SnapshotManifest(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-0123456789ab-r1",
        upstream_commit="b" * 40,
        builder_commit="c" * 40,
        target="x86_64-unknown-linux-gnu",
        workflow=WorkflowIdentity(
            path=".github/workflows/kani-monthly.yml",
            event="workflow_dispatch",
            ref="refs/heads/main",
            run_id=1,
            run_attempt=1,
        ),
        toolchain=ToolchainIdentity(
            identifier="nightly-2026-09-23",
            rustc_vv="rustc 1.100.0-nightly (abc 2026-09-23)",
            kani_compiler="kani-compiler 0.66.0",
        ),
        launcher_package_version="0.66.0",
        backend=BackendIdentity(version="0.66.0", revision="b" * 40),
        runtime=RuntimeIdentity(glibc_baseline="2.34"),
        build=BuildInputs(submodules=(), lock_digests=(), recipe_digest="e" * 64),
        prerequisites=(),
        cache=CacheObservation(outcome="miss"),
        resources=ResourceObservation(
            wall_seconds=0.0,
            disk_high_water_bytes=0,
            asset_bytes=0,
        ),
        assets=(),
    )


def test_identity_directory_is_stable_and_identity_scoped(tmp_path: Path) -> None:
    """The identity key, not the reported version, selects the directory."""
    first = identity_directory(_pin(), cache_home=tmp_path)
    again = identity_directory(_pin(), cache_home=tmp_path)
    other = identity_directory(_pin("d" * 64), cache_home=tmp_path)

    assert first == again
    assert first.name == identity_key(_pin())
    assert other.name == identity_key(_pin("d" * 64))
    assert first.parent == tmp_path


def test_identity_directory_uses_xdg_cache_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit cache home wins; otherwise XDG_CACHE_HOME is honoured."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    assert identity_directory(_pin()).parent == tmp_path / "xdg"


def test_generation_names_are_unique_and_safe() -> None:
    """A fresh generation name cannot collide with or traverse a sibling."""
    first = new_generation_name()
    second = new_generation_name()

    assert first != second
    assert is_safe_generation_name(first)
    assert is_safe_generation_name(second)


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../escape", "a/b", "a\\b", "a\x00b", "a b"]
)
def test_unsafe_generation_names_are_rejected(name: str) -> None:
    """Generation names are a single plain path component."""
    assert not is_safe_generation_name(name)
    with pytest.raises(ProverToolError, match="generation"):
        snapshot_layout(_pin(), cache_home=typ.cast("Path", None), generation=name)


def test_snapshot_layout_places_everything_inside_the_generation(
    tmp_path: Path,
) -> None:
    """Mutable homes, receipt and launcher share one private generation."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")

    assert layout.generation_dir == layout.identity_dir / "generations" / "g1"
    assert layout.kani_home == layout.generation_dir / "kani-home"
    assert layout.rustup_home == layout.generation_dir / "rustup-home"
    assert layout.cargo_home == layout.generation_dir / "cargo-home"
    assert layout.receipt_path == layout.generation_dir / "receipt.json"
    assert layout.active_path == layout.identity_dir / "active.json"
    assert layout.lock_path == layout.identity_dir / "install.lock"
    for path in (
        layout.kani_home,
        layout.rustup_home,
        layout.cargo_home,
        layout.receipt_path,
    ):
        assert path.is_relative_to(layout.generation_dir)


def test_runtime_selects_the_exact_backend_and_launcher(tmp_path: Path) -> None:
    """The runtime derives absolute launcher, driver and compiler paths."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    runtime = runtime_for(layout, _manifest())

    kani_dir = layout.kani_home / "kani-0.66.0"
    assert runtime.kani_dir == kani_dir
    assert runtime.launcher == kani_dir / "bin" / "kani"
    assert runtime.driver == kani_dir / "bin" / "kani-driver"
    assert runtime.compiler == kani_dir / "bin" / "kani-compiler"
    assert runtime.rustc == layout.rustup_home / "toolchains" / (
        "nightly-2026-09-23/bin/rustc"
    )


def test_execution_environment_replaces_selection_and_keeps_wrappers(
    tmp_path: Path,
) -> None:
    """Proofs see only generation-private homes but keep admission wrappers."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    runtime = runtime_for(layout, _manifest())
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/example",
        "RUSTC": "/opt/fake/rustc",
        "RUSTFLAGS": "-Ctarget-cpu=native",
        "RUSTUP_TOOLCHAIN": "stable",
        "RUSTUP_DIST_SERVER": "https://example.invalid",
        "RUSTUP_UPDATE_ROOT": "https://example.invalid",
        "RUSTUP_DIST_ROOT": "https://example.invalid",
        "RUSTC_WRAPPER": "/usr/local/bin/sccache",
        "RUSTC_WORKSPACE_WRAPPER": "/usr/local/bin/build-limits-rustc",
        "RUSTDOC": "/usr/local/bin/rustdoc-wrapper",
        "KANI_HOME": "/home/example/.kani",
        "UNRELATED": "kept",
    }

    env = execution_environment(runtime, base)

    assert env["KANI_HOME"] == str(layout.kani_home)
    assert env["RUSTUP_HOME"] == str(layout.rustup_home)
    assert env["CARGO_HOME"] == str(layout.cargo_home)
    assert env["RUSTUP_TOOLCHAIN"] == "nightly-2026-09-23"
    assert env["RUSTUP_AUTO_INSTALL"] == "0"
    # The result is an overlay: unrelated inherited values are simply absent
    # from it and keep flowing to the child through Cuprum.
    assert "UNRELATED" not in env
    assert "HOME" not in env
    # Admission wrappers and `RUSTDOC` are not mentioned, so the overlay
    # leaves them inherited rather than removing them from the child.
    for preserved in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"):
        assert preserved not in env, f"{preserved} was removed from the child"
    for removed in (
        "RUSTC",
        "RUSTFLAGS",
        "RUSTUP_DIST_SERVER",
        "RUSTUP_UPDATE_ROOT",
        "RUSTUP_DIST_ROOT",
    ):
        assert env[removed] is UNSET, f"{removed} would still be inherited"
    child_path = env["PATH"]
    assert isinstance(child_path, str)
    assert child_path.split(":")[:2] == [
        str(runtime.kani_dir / "bin"),
        str(runtime.kani_dir / "pyroot" / "bin"),
    ]
    assert child_path.endswith(":/usr/bin:/bin")


def test_execution_environment_removes_selectors_from_a_real_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual child process cannot see an inherited distribution selector."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    runtime = runtime_for(layout, _manifest())
    removed = (
        "RUSTC",
        "RUSTFLAGS",
        "RUSTUP_DIST_SERVER",
        "RUSTUP_UPDATE_ROOT",
        "RUSTUP_DIST_ROOT",
    )
    for name in removed:
        monkeypatch.setenv(name, f"host-{name}")
    preserved = {
        "RUSTC_WRAPPER": "/usr/local/bin/sccache",
        "RUSTC_WORKSPACE_WRAPPER": "/usr/local/bin/build-limits-rustc",
        "RUSTDOC": "/usr/local/bin/rustdoc-wrapper",
    }
    for name, value in preserved.items():
        monkeypatch.setenv(name, value)
    env = execution_environment(runtime, os.environ)
    wanted = (*removed, *preserved, "KANI_HOME", "RUSTUP_TOOLCHAIN")
    script = (
        "import json, os; "
        f"names = {wanted!r}; "
        "print(json.dumps({name: os.environ.get(name) for name in names}))"
    )

    result = run_command(CommandSpec((sys.executable, "-I", "-c", script), env=env))
    observed = json.loads(result.stdout)

    assert all(observed[name] is None for name in removed), (
        "a host compiler or distribution selector reached the child"
    )
    assert all(observed[name] == value for name, value in preserved.items()), (
        "a preserved admission wrapper did not reach the child"
    )
    assert observed["KANI_HOME"] == str(layout.kani_home)
    assert observed["RUSTUP_TOOLCHAIN"] == "nightly-2026-09-23"
    assert all(os.environ[name] == f"host-{name}" for name in removed), (
        "the parent environment was mutated"
    )


def test_execution_environment_does_not_duplicate_its_own_entries(
    tmp_path: Path,
) -> None:
    """A repeated install must not grow PATH or PYTHONPATH forever."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    runtime = runtime_for(layout, _manifest())
    first = execution_environment(runtime, {"PATH": "/usr/bin"})
    second = execution_environment(runtime, first)

    assert first["PATH"] == second["PATH"]
    assert (
        first["PYTHONPATH"] == second["PYTHONPATH"] == str(runtime.kani_dir / "pyroot")
    )


@pytest.mark.parametrize(
    "args",
    [
        ("setup",),
        ("kani", "setup"),
        ("kani", "setup", "--use-local-bundle", "b.tar.gz"),
        ("--use-local-bundle", "b.tar.gz"),
        ("--use-local-bundle=b.tar.gz",),
        ("--use-local-toolchain", "/opt/toolchain"),
        ("-Z", "lean"),
        ("-Zlean",),
        ("-Z=lean",),
        ("--harness", "x", "-Z", "lean"),
    ],
)
def test_setup_and_backend_selector_arguments_are_rejected(
    args: tuple[str, ...],
) -> None:
    """The wrapper never re-runs setup or swaps the Kani backend."""
    with pytest.raises(ProverToolError, match="snapshot"):
        validate_forwarded_args(args)


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("src/lib.rs",),
        ("--harness", "proofs::ok"),
        ("--solver", "kissat"),
        ("-Z", "function-contracts", "--harness", "proofs::ok"),
        ("-Zfunction-contracts",),
        ("--cbmc-args", "--unwind", "4"),
    ],
)
def test_ordinary_verification_arguments_are_forwarded(
    args: tuple[str, ...],
) -> None:
    """Harness, solver and feature options remain transparent."""
    validate_forwarded_args(args)


def test_identity_report_states_exact_paths_and_compiler(
    tmp_path: Path,
) -> None:
    """The check output names the generation, launcher and compiler identity."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    runtime = runtime_for(layout, _manifest())

    report = build_identity_report(_pin(), _manifest(), runtime)

    assert report.tag == _pin().tag
    assert report.manifest_sha256 == _SHA
    assert report.target == "x86_64-unknown-linux-gnu"
    assert report.launcher == runtime.launcher
    assert report.driver == runtime.driver
    assert report.compiler == runtime.compiler
    assert report.toolchain == "nightly-2026-09-23"
    assert "rustc 1.100.0-nightly" in report.rustc_vv
    assert all(path.is_absolute() for path in (report.launcher, report.driver))


def test_layout_paths_are_absolute_and_private(tmp_path: Path) -> None:
    """A layout never hands a relative path to setup or execution."""
    layout = snapshot_layout(_pin(), cache_home=tmp_path, generation="g1")
    values = [
        layout.identity_dir,
        layout.generation_dir,
        layout.kani_home,
        layout.rustup_home,
        layout.cargo_home,
        layout.receipt_path,
        layout.active_path,
        layout.lock_path,
    ]

    assert all(path.is_absolute() for path in values)
    assert isinstance(layout, SnapshotLayout)
