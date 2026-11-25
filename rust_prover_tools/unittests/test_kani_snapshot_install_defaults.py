"""Contract tests for the production snapshot setup step.

Setup runs the packaged launcher with generation-private homes. Rustup's
automatic self-update is not wanted there: it expects the `rustup` binary to
live in `CARGO_HOME`, which is the generation's private cargo home, so a
normal local install would fail after the toolchain was already installed.
The configuration step must therefore run first, with the same homes.
"""

from __future__ import annotations

import inspect
import time
import typing as typ

import pytest
from cuprum.context import UNSET

from rust_prover_tools import commands
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import install_defaults
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime
from tests.kani_snapshot_bundles import TOOLCHAIN, VERSION
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.context import UnsetType


def _runtime(tmp_path: Path) -> SnapshotRuntime:
    """Return a generation runtime with the private homes the step uses."""
    return SnapshotRuntime(
        layout=snapshot_layout(make_pin(), cache_home=tmp_path, generation="gen"),
        toolchain=TOOLCHAIN,
        backend_version=VERSION,
    )


def _homes(
    spec: commands.CommandSpec,
) -> cabc.Mapping[str, str | UnsetType]:
    """Return one command's environment mapping, refusing an absent one."""
    assert spec.env is not None, "the setup command must state its environment"
    return spec.env


def _record_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> list[commands.CommandSpec]:
    """Replace command execution and record every command spec."""
    seen: list[commands.CommandSpec] = []

    def fake_run_checked(spec: commands.CommandSpec) -> commands.CommandResult:
        """Record the spec and report success."""
        seen.append(spec)
        return commands.CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(install_defaults, "run_checked", fake_run_checked)
    return seen


def test_setup_disables_rustup_self_update_before_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private cargo home is configured before any toolchain work starts."""
    seen = _record_commands(monkeypatch)
    runtime = _runtime(tmp_path)
    bundle = tmp_path / "bundle.tar.gz"

    install_defaults.run_local_bundle_setup(runtime, bundle)

    assert [spec.argv[:4] for spec in seen] == [
        ("rustup", "set", "auto-self-update", "disable"),
        (
            str(runtime.layout.launcher_dir / "bin" / "cargo-kani"),
            "setup",
            "--use-local-bundle",
            str(bundle),
        ),
    ]
    homes = _homes(seen[0])
    assert homes["RUSTUP_HOME"] == str(runtime.layout.rustup_home)
    assert homes["CARGO_HOME"] == str(runtime.layout.cargo_home)
    assert _homes(seen[1])["RUSTUP_HOME"] == str(runtime.layout.rustup_home)


def test_setup_removes_an_inherited_tar_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher's extraction still refuses an inherited tar policy."""
    seen = _record_commands(monkeypatch)

    install_defaults.run_local_bundle_setup(_runtime(tmp_path), tmp_path / "b.tar.gz")

    assert _homes(seen[-1])["TAR_OPTIONS"] is UNSET, (
        "an inherited TAR_OPTIONS must be unset, not merely overridden"
    )


def test_the_launcher_receives_only_the_remaining_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow configuration command shortens the launcher's timeout.

    The clock is injected rather than read from the module, so this test proves
    the budget arithmetic consults the caller's clock and nothing else.
    """
    seen = _record_commands(monkeypatch)
    ticks = iter([100.0, 104.0])

    install_defaults.run_local_bundle_setup(
        _runtime(tmp_path),
        tmp_path / "bundle.tar.gz",
        timeout_seconds=10.0,
        clock=lambda: next(ticks),
    )

    assert seen[0].timeout_seconds == pytest.approx(10.0), (
        "the first command gets the full budget"
    )
    assert seen[1].timeout_seconds == pytest.approx(6.0), (
        "the launcher gets only what remains"
    )


def test_an_expired_setup_budget_refuses_to_start_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is checked before the launcher starts, not only during it."""
    seen = _record_commands(monkeypatch)
    ticks = iter([100.0, 111.0])

    with pytest.raises(ProverToolError, match="budget"):
        install_defaults.run_local_bundle_setup(
            _runtime(tmp_path),
            tmp_path / "bundle.tar.gz",
            timeout_seconds=10.0,
            clock=lambda: next(ticks),
        )

    assert len(seen) == 1, "the launcher must not start after the budget expires"


def test_the_default_clock_is_the_monotonic_clock() -> None:
    """Production reads no wall clock, so an injected clock is the only seam."""
    default_clock = (
        inspect
        .signature(install_defaults.run_local_bundle_setup)
        .parameters["clock"]
        .default
    )

    assert default_clock is time.monotonic, (
        "setup must default to the monotonic clock, not a wall clock"
    )


def test_the_rustup_configuration_step_is_named_and_documented() -> None:
    """The side effect is its own function, so a reader can see what it changes."""
    configure = getattr(install_defaults, "disable_rustup_self_update", None)

    assert configure is not None, (
        "the rustup configuration step must be its own named function"
    )
    assert configure.__doc__, "the configuration step must state its side effect"
    assert "self-update" in configure.__doc__, (
        "the docstring must name the setting it changes"
    )
