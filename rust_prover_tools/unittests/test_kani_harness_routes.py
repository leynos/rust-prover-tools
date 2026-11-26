"""Hold the qualify-once seams to one probe sequence and one generation."""

from __future__ import annotations

import os
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
    ProofContext,
    SnapshotOutput,
    prepare_snapshot_proof,
)
from rust_prover_tools.kani_snapshot.run import (
    RunOptions as SnapshotRunOptions,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two harness commands share one activation read and one environment."""
    from cuprum.context import EnvMode

    options, hooks = installed_snapshot
    from rust_prover_tools.kani_snapshot import run as snapshot_run

    monkeypatch.setenv("RPT_FROZEN_HOST", "1")

    counted = mock.Mock(wraps=snapshot_run.check_active_snapshot)
    monkeypatch.setattr(snapshot_run, "check_active_snapshot", counted)

    prepared = prepare_snapshot_proof(
        SnapshotRunOptions(
            pin=options.pin,
            args=(),
            cache_home=options.cache_home,
        ),
        hooks,
        context=ProofContext(cwd=tmp_path, environment=os.environ),
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
    assert first.env_mode is EnvMode.REPLACE
    assert first.env is not None
    assert first.env["RPT_FROZEN_HOST"] == "1"
    monkeypatch.setenv("RPT_LATER_HOST", "2")
    assert "RPT_LATER_HOST" not in first.env


def test_the_default_snapshot_probe_uses_the_supplied_environment(
    installed_snapshot: tuple[SnapshotRunOptions, InstallHooks],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default probe reads the supplied mapping, not the process one."""
    from rust_prover_tools.kani_snapshot import check as snapshot_check
    from rust_prover_tools.kani_snapshot.run import (
        ProofContext,
        prepare_snapshot_proof,
    )

    options, _hooks = installed_snapshot
    seen: list[dict[str, str]] = []

    class _ProbeSeenError(Exception):
        """Stop the check once the probe has recorded its environment."""

    def recording(
        runtime: object,
        timeout_seconds: float | None = None,
        *,
        environment: cabc.Mapping[str, str],
    ) -> object:
        """Record the probe's environment and stop the check."""
        del runtime, timeout_seconds
        seen.append(dict(environment))
        raise _ProbeSeenError

    monkeypatch.setattr(snapshot_check, "probe_generation", recording)
    supplied = {**os.environ, "RUSTC_WRAPPER": "supplied-wrapper"}

    with pytest.raises(_ProbeSeenError):
        prepare_snapshot_proof(
            SnapshotRunOptions(
                pin=options.pin,
                args=(),
                cache_home=options.cache_home,
            ),
            None,
            context=ProofContext(cwd=tmp_path, environment=supplied),
            validate_arguments=False,
        )

    assert seen
    assert seen[0]["RUSTC_WRAPPER"] == "supplied-wrapper"


def test_the_snapshot_route_uses_only_the_supplied_environment(
    installed_snapshot: tuple[SnapshotRunOptions, InstallHooks],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied environment reaches snapshot qualification unchanged.

    The process environment holds a conflicting marker; the supplied mapping
    must win, and a supplied-only marker must survive into the composed
    command.
    """
    from rust_prover_tools.kani_cli_grammar import KANI_067
    from rust_prover_tools.kani_inventory import InventoryHarness
    from rust_prover_tools.kani_routes import SnapshotRoute, prepare_route

    options, hooks = installed_snapshot
    monkeypatch.setattr(
        "rust_prover_tools.kani_routes.grammar_for",
        lambda version: KANI_067,
    )
    monkeypatch.setenv("RPT_FROZEN_HOST", "from-the-process")
    supplied = {
        **os.environ,
        "RPT_FROZEN_HOST": "supplied",
        "RPT_SUPPLIED_ONLY": "1",
    }

    prepared = prepare_route(
        SnapshotRoute(
            pin=options.pin,
            cache_home=options.cache_home,
            hooks=hooks,
        ),
        environ=supplied,
        cwd=tmp_path,
    )
    harness = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harness",
        source="src/lib.rs",
        line=1,
    )

    command = prepared.build(harness)

    assert command.env is not None
    assert command.env.values["RPT_FROZEN_HOST"] == "supplied"
    assert command.env.values["RPT_SUPPLIED_ONLY"] == "1"


def test_a_snapshot_route_environment_wins_over_the_provider(
    installed_snapshot: tuple[SnapshotRunOptions, InstallHooks],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit route environment takes precedence over the provider."""
    from rust_prover_tools.kani_cli_grammar import KANI_067
    from rust_prover_tools.kani_inventory import InventoryHarness
    from rust_prover_tools.kani_routes import SnapshotRoute, prepare_route

    options, hooks = installed_snapshot
    monkeypatch.setattr(
        "rust_prover_tools.kani_routes.grammar_for",
        lambda version: KANI_067,
    )
    route_environment = {**os.environ, "RPT_FROZEN_HOST": "route"}

    prepared = prepare_route(
        SnapshotRoute(
            pin=options.pin,
            cache_home=options.cache_home,
            hooks=hooks,
            environment=route_environment,
        ),
        environ={**os.environ, "RPT_FROZEN_HOST": "provider"},
        cwd=tmp_path,
    )
    harness = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harness",
        source="src/lib.rs",
        line=1,
    )

    command = prepared.build(harness)

    assert command.env is not None
    assert command.env.values["RPT_FROZEN_HOST"] == "route"


def test_an_explicit_empty_snapshot_route_environment_is_not_replaced(
    installed_snapshot: tuple[SnapshotRunOptions, InstallHooks],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly empty route environment is not swapped for the provider."""
    from rust_prover_tools.kani_cli_grammar import KANI_067
    from rust_prover_tools.kani_inventory import InventoryHarness
    from rust_prover_tools.kani_routes import SnapshotRoute, prepare_route

    options, hooks = installed_snapshot
    monkeypatch.setattr(
        "rust_prover_tools.kani_routes.grammar_for",
        lambda version: KANI_067,
    )

    prepared = prepare_route(
        SnapshotRoute(
            pin=options.pin,
            cache_home=options.cache_home,
            hooks=hooks,
            environment={},
        ),
        environ={**os.environ, "RPT_PROVIDER_ONLY": "1"},
        cwd=tmp_path,
    )
    harness = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harness",
        source="src/lib.rs",
        line=1,
    )

    command = prepared.build(harness)

    assert command.env is not None
    assert "RPT_PROVIDER_ONLY" not in command.env.values


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


def test_the_legacy_route_uses_only_the_supplied_environment(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualification consumes the supplied mapping, not the process environment.

    The provider is passed explicitly, so a marker that exists only in the
    process environment must not reach the composed command, while a marker in
    the supplied mapping must.
    """
    from rust_prover_tools.kani_inventory import InventoryHarness
    from rust_prover_tools.kani_routes import LegacyRoute, prepare_route

    seen: list[str] = []

    def probing_runner(spec: CommandSpec) -> CommandResult:
        """Record the HOME the qualification probes received."""
        environment = spec.env or {}
        seen.append(str(environment.get("HOME", "")))
        program = spec.argv[0]
        if program.endswith("kani-driver"):
            return CommandResult("kani 0.67.0\n", "", 0)
        return CommandResult("rustc 1.93.0-nightly (53732d5e0 2025-11-20)\n", "", 0)

    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        probing_runner,
    )
    monkeypatch.setenv("HOME", "/from-the-process")
    supplied = str(tmp_path / "supplied-home")

    prepared = prepare_route(
        LegacyRoute(installation=installed_release, cwd=tmp_path),
        environ={"HOME": supplied, "PATH": "/usr/bin"},
    )
    harness = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harness",
        source="src/lib.rs",
        line=1,
    )

    command = prepared.build(harness)

    assert seen
    assert all(value == supplied for value in seen)
    assert command.env is not None
    assert command.env.values["HOME"] == supplied


def test_the_supervised_legacy_build_freezes_replacement_environment(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived legacy command replaces, rather than overlays, its environment."""
    from rust_prover_tools.kani_inventory import InventoryHarness
    from rust_prover_tools.kani_routes import LegacyRoute, prepare_route

    monkeypatch.setattr(
        "rust_prover_tools.kani_release.run_command",
        _identity_runner([]),
    )
    prepared = prepare_route(LegacyRoute(installation=installed_release, cwd=tmp_path))
    harness = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harness",
        source="src/lib.rs",
        line=1,
    )

    command = prepared.build(harness)

    assert command.env is not None
    assert command.env.replace is True


def test_identity_probes_preserve_the_prepared_environment_mode(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probes inspect the same environment as the proof command."""
    from cuprum.context import EnvMode

    specs: list[CommandSpec] = []

    def run(spec: CommandSpec) -> CommandResult:
        """Record each probe specification and answer it as qualified."""
        specs.append(spec)
        return _identity_runner([])(spec)

    monkeypatch.setattr("rust_prover_tools.kani_release.run_command", run)

    qualify_kani_installation(
        KaniExecOptions(installed_release, tmp_path, ()),
        {"HOME": str(tmp_path)},
    )

    assert specs
    assert all(spec.env_mode is EnvMode.REPLACE for spec in specs)
