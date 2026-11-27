"""Hold the CLI-only signal adapter to its installation and event contract."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import typing as typ

from rust_prover_tools.kani_harness_cli import (
    SignalInterrupt,
    install_interrupt_handlers,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest

    from rust_prover_tools.kani_harness_run import (
        KaniHarnessDependencies,
        KaniHarnessPreparation,
        KaniHarnessRun,
        KaniHarnessRunOptions,
    )
    from rust_prover_tools.supervision import Diagnostics, SupervisionConfig


def test_a_signal_before_binding_is_already_recorded() -> None:
    """A signal that arrives first still stops the run at its first check."""
    interrupt = SignalInterrupt()

    interrupt._handle(signal.SIGTERM, None)

    assert interrupt.signum == signal.SIGTERM
    asyncio.run(asyncio.wait_for(interrupt.wait(), timeout=1))


def test_binding_releases_a_waiter_on_a_signal() -> None:
    """The handler schedules the event on the bound loop."""
    interrupt = SignalInterrupt()
    loop = asyncio.new_event_loop()

    async def scenario() -> None:
        """Start a waiter, then deliver the signal that must release it."""
        waiter = asyncio.create_task(interrupt.wait())
        await asyncio.sleep(0)
        interrupt._handle(signal.SIGINT, None)
        await asyncio.wait_for(waiter, timeout=1)

    try:
        interrupt.bind(loop)
        loop.run_until_complete(scenario())
    finally:
        loop.close()

    assert interrupt.signum == signal.SIGINT


def test_installation_restores_the_previous_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI restores whatever policy the embedding application had."""
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr("signal.getsignal", lambda sig: f"previous-{sig}")
    monkeypatch.setattr(
        "signal.signal", lambda sig, handler: calls.append((sig, handler))
    )
    interrupt = SignalInterrupt()

    with install_interrupt_handlers(interrupt):
        assert [sig for sig, _ in calls] == [signal.SIGINT, signal.SIGTERM]

    assert calls[-2:] == [
        (signal.SIGINT, f"previous-{signal.SIGINT}"),
        (signal.SIGTERM, f"previous-{signal.SIGTERM}"),
    ]


def test_the_first_signal_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later signal does not replace the reason the run stopped."""
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr("signal.getsignal", lambda sig: None)
    monkeypatch.setattr(
        "signal.signal", lambda sig, handler: calls.append((sig, handler))
    )
    interrupt = SignalInterrupt()

    with install_interrupt_handlers(interrupt):
        interrupt._handle(signal.SIGINT, None)
        interrupt._handle(signal.SIGTERM, None)

    assert interrupt.signum == signal.SIGINT


def test_the_cli_installs_handlers_only_for_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery and qualification keep the application's signal policy."""
    from rust_prover_tools.cli import app
    from tests.kani_harness_cli_support import inventory_file
    from tests.kani_harness_cli_support import run as make_run

    state: list[str] = []

    @contextlib.contextmanager
    def fake_handlers(interrupt: object) -> cabc.Generator[None]:
        """Mark the execution phase as guarded."""
        del interrupt
        state.append("installed")
        try:
            yield
        finally:
            state.append("restored")

    def fake_prepare(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
        """Refuse to be called with handlers already installed."""
        del options
        assert not state
        return typ.cast("KaniHarnessPreparation", object())

    def fake_execute(
        preparation: KaniHarnessPreparation,
        config: SupervisionConfig,
        dependencies: KaniHarnessDependencies | None = None,
        *,
        diagnostics: Diagnostics | None = None,
    ) -> KaniHarnessRun:
        """Require the execution phase to be guarded."""
        del preparation, config, dependencies, diagnostics
        assert state == ["installed"]
        return make_run()

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.install_interrupt_handlers",
        fake_handlers,
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.prepare_harnesses", fake_prepare
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.execute_harnesses", fake_execute
    )

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory_file(tmp_path)),
    ])

    assert status == 0
    assert state == ["installed", "restored"]


def test_a_signal_during_inventory_discovery_ends_the_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery interrupted by SIGINT ends the run before execution."""
    evidence = tmp_path / "evidence.jsonl"

    def interrupted(
        root: Path,
        packages: tuple[str, ...],
        *,
        metadata: object = None,
    ) -> object:
        """Stand in for discovery interrupted by SIGINT."""
        del root, packages, metadata
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_run.cargo_metadata",
        lambda root: (),
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_run.discover_inventory", interrupted
    )

    status = _run_cli(tmp_path, evidence, use_inventory=False)

    _assert_interrupted(status, evidence, capsys)


def test_a_signal_during_route_qualification_ends_the_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualification interrupted by SIGINT ends the run before execution."""
    evidence = tmp_path / "evidence.jsonl"

    def interrupted(
        route: object,
        *,
        environ: object = None,
        cwd: object = None,
    ) -> object:
        """Stand in for qualification interrupted by SIGINT."""
        del route, environ, cwd
        raise KeyboardInterrupt

    monkeypatch.setattr("rust_prover_tools.kani_harness_run.prepare_route", interrupted)

    status = _run_cli(tmp_path, evidence, use_inventory=True)

    _assert_interrupted(status, evidence, capsys)


def _assert_interrupted(
    status: int,
    evidence: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Require the interruption status and an untouched evidence path."""
    assert status == 128 + signal.SIGINT
    assert not evidence.exists()
    assert "interrupted" in capsys.readouterr().err


def _run_cli(tmp_path: Path, evidence: Path, *, use_inventory: bool) -> int:
    """Run a CLI command whose preparation phase the test interrupts."""
    from rust_prover_tools.cli import app
    from tests.kani_harness_cli_support import inventory_file

    arguments = [
        "kani",
        "harnesses",
        "run",
        "--repo-root",
        str(tmp_path),
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--jsonl",
        str(evidence),
    ]
    if use_inventory:
        arguments.extend(("--inventory", str(inventory_file(tmp_path))))
    return app(arguments)


def test_a_signal_after_the_loop_closed_is_still_recorded() -> None:
    """A late signal records its reason without touching a closed loop."""
    interrupt = SignalInterrupt()
    loop = asyncio.new_event_loop()
    interrupt.bind(loop)
    loop.close()

    interrupt._handle(signal.SIGTERM, None)

    assert interrupt.signum == signal.SIGTERM


def test_an_execution_keyboard_interrupt_is_named(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected SIGINT during execution is not called preparation."""
    from rust_prover_tools.cli import app
    from tests.kani_harness_cli_support import inventory_file

    def prepared(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
        """Reach execution without qualifying a real installation."""
        del options
        return typ.cast("KaniHarnessPreparation", object())

    def interrupted(
        preparation: KaniHarnessPreparation,
        config: SupervisionConfig,
        dependencies: KaniHarnessDependencies | None = None,
        *,
        diagnostics: Diagnostics | None = None,
    ) -> KaniHarnessRun:
        """Raise the interrupt the handler would normally replace."""
        del preparation, config, dependencies, diagnostics
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.prepare_harnesses", prepared
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.execute_harnesses", interrupted
    )

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory_file(tmp_path)),
    ])

    assert status == 128 + signal.SIGINT
    assert "execution interrupted" in capsys.readouterr().err


def test_a_signal_recorded_after_the_report_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late SIGTERM is not hidden by a completed passing report."""
    from rust_prover_tools.cli import app
    from tests.kani_harness_cli_support import inventory_file
    from tests.kani_harness_cli_support import run as make_run

    def prepared(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
        """Reach execution without qualifying a real installation."""
        del options
        return typ.cast("KaniHarnessPreparation", object())

    def execute(
        preparation: KaniHarnessPreparation,
        config: SupervisionConfig,
        dependencies: KaniHarnessDependencies | None = None,
        *,
        diagnostics: Diagnostics | None = None,
    ) -> KaniHarnessRun:
        """Record a signal after producing the completed report."""
        del preparation, config, diagnostics
        assert dependencies is not None
        interrupt = typ.cast("SignalInterrupt", dependencies.interrupts)
        interrupt._handle(signal.SIGTERM, None)
        return make_run()

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.prepare_harnesses", prepared
    )
    monkeypatch.setattr("rust_prover_tools.kani_harness_cli.execute_harnesses", execute)

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory_file(tmp_path)),
    ])

    assert status == 128 + signal.SIGTERM
