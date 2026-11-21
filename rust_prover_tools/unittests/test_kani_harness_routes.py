"""Hold the qualify-once seams to one probe sequence and one generation."""

from __future__ import annotations

import typing as typ
from unittest import mock

import pytest

from rust_prover_tools.commands import CommandResult
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_qualification import (
    qualify_kani_installation,
)
from rust_prover_tools.kani_release import (
    KaniExecOptions,
    exec_kani,
    validate_kani_arguments,
)
from rust_prover_tools.kani_snapshot.run import (
    RunOptions as SnapshotRunOptions,
)
from rust_prover_tools.kani_snapshot.run import (
    SnapshotOutput,
    prepare_snapshot_proof,
)
from tests.kani_snapshot_install_fixtures import (
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.commands import CommandSpec
    from rust_prover_tools.kani_snapshot.install_model import InstallHooks


@pytest.fixture
def installed_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Create a release tree with the exact qualified metadata."""
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.platform.machine", lambda: "x86_64"
    )
    root = tmp_path / "kani-0.67.0"
    for directory in ("bin", "toolchain/bin", "toolchain/lib"):
        (root / directory).mkdir(parents=True)
    (root / "rustc-version").write_text(
        "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\n", encoding="utf-8"
    )
    (root / "rust-toolchain-version").write_text(
        "nightly-2025-11-21-x86_64-unknown-linux-gnu\n", encoding="utf-8"
    )
    for name in (
        "bin/kani-driver",
        "bin/kani-compiler",
        "toolchain/bin/rustc",
        "toolchain/bin/cargo",
    ):
        path = root / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    (root / "toolchain/lib/libLLVM.so.21.1-rust-1.93.0-nightly").touch()
    return root


def _identity_runner(
    calls: list[tuple[str, ...]],
) -> cabc.Callable[[CommandSpec], CommandResult]:
    """Return a command double that answers the three identity probes."""

    def run(spec: CommandSpec) -> CommandResult:
        """Answer one identity probe and record its argument vector."""
        calls.append(spec.argv)
        program = spec.argv[0]
        if program.endswith("kani-driver"):
            return CommandResult("kani 0.67.0\n", "", 0)
        return CommandResult("rustc 1.93.0-nightly (53732d5e0 2025-11-20)\n", "", 0)

    return run


def test_qualification_probes_once_and_reuses_the_base(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One installation, environment, and identity probe sequence per run."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner(calls),
    )

    qualified = qualify_kani_installation(
        KaniExecOptions(installed_release, tmp_path, ()),
        {"HOME": str(tmp_path)},
    )
    first = qualified.command(("--package", "a", "--harness", "h::x"))
    second = qualified.command(("--package", "b", "--harness", "h::y"))

    assert len(calls) == 3, "qualification probed more than once"
    assert qualified.base_argv == (str(installed_release / "bin/kani-driver"), "kani")
    assert first.argv[-4:] == ("--package", "a", "--harness", "h::x")
    assert second.argv[-4:] == ("--package", "b", "--harness", "h::y")
    assert first.env is second.env, "the environment was rebuilt per harness"
    assert first.cwd == second.cwd == tmp_path.resolve()


def test_exec_kani_keeps_the_documented_stage_order(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extracting the seam must not reorder or drop a stage event."""
    stages: list[str] = []
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner(calls),
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.emit_stage_event",
        lambda stage, started, fields: stages.append(stage),
    )

    status = exec_kani(
        KaniExecOptions(installed_release, tmp_path, ("--verbose",)),
        {"HOME": str(tmp_path)},
    )

    assert status == 0
    assert stages == [
        "validation",
        "rustc_identity",
        "compiler_identity",
        "verifier_identity",
        "proof",
    ]
    assert calls[-1][-1] == "--verbose"


def test_the_legacy_route_still_rejects_an_empty_argument_vector(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-invocation route keeps its original refusal."""
    from rust_prover_tools.kani_release import prepare_kani_command

    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner([]),
    )

    with pytest.raises(ProverToolError, match="needs verifier arguments"):
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ()),
            {"HOME": str(tmp_path)},
        )


def test_the_composed_argument_validator_keeps_the_routing_contract() -> None:
    """A composed vector cannot add setup or a Cargo routing override."""
    for argument in ("--config", "--profile", "--manifest-path"):
        with pytest.raises(ProverToolError, match="without setup"):
            validate_kani_arguments((argument, "x"))
    with pytest.raises(ProverToolError, match="without setup"):
        validate_kani_arguments(("setup",))
    validate_kani_arguments(("--package", "p", "--harness", "h", "--exact"))


@pytest.fixture
def installed_snapshot(tmp_path: Path) -> tuple[SnapshotRunOptions, InstallHooks]:
    """Install the fixture snapshot and return run options and hooks."""
    from rust_prover_tools.kani_snapshot.install import install_snapshot

    case = make_case(tmp_path)
    hooks = install_hooks(case)
    install_snapshot(install_options(case), hooks)
    return SnapshotRunOptions(pin=case.pin, cache_home=case.cache_home), hooks


def test_the_snapshot_route_resolves_one_generation(
    installed_snapshot: tuple[SnapshotRunOptions, InstallHooks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two harness commands share one activation read and one environment."""
    options, hooks = installed_snapshot
    from rust_prover_tools.kani_snapshot import run as snapshot_run

    counted = mock.Mock(wraps=snapshot_run.check_active_snapshot)
    monkeypatch.setattr(snapshot_run, "check_active_snapshot", counted)

    prepared = prepare_snapshot_proof(
        SnapshotRunOptions(
            pin=options.pin,
            args=(),
            cache_home=options.cache_home,
        ),
        hooks,
        validate_arguments=False,
    )
    first = prepared.command(
        ("--package", "a", "--harness", "h::x"),
        SnapshotOutput(echo=False, capture=False),
    )
    second = prepared.command(
        ("--package", "b", "--harness", "h::y"),
        SnapshotOutput(echo=False, capture=False),
    )

    assert counted.call_count == 1, "the active pointer was read more than once"
    assert first.argv[0] == second.argv[0] == str(prepared.runtime.launcher)
    assert first.env is second.env, "the proof environment was rebuilt"


def test_an_explicit_empty_environment_is_not_replaced(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only None inherits the process environment."""
    monkeypatch.setenv("RPT_HOST_ONLY", "1")
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner([]),
    )

    qualified = qualify_kani_installation(
        KaniExecOptions(installed_release, tmp_path, ()),
        {"HOME": str(tmp_path)},
    )

    environment = qualified.spec.env
    assert environment is not None
    assert "RPT_HOST_ONLY" not in environment


def test_an_empty_environment_is_not_replaced_by_the_host(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty injected environment fails rather than reading the host."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RPT_HOST_ONLY", "1")
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner([]),
    )

    with pytest.raises(ProverToolError, match="requires injected HOME or CARGO_HOME"):
        qualify_kani_installation(
            KaniExecOptions(installed_release, tmp_path, ()),
            {},
        )
