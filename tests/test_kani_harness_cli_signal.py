"""Hold the CLI-only signal adapter to its installation and event contract."""

from __future__ import annotations

import asyncio
import signal
import typing as typ

from rust_prover_tools.kani_harness_cli import SignalInterrupt

if typ.TYPE_CHECKING:
    import pytest


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
    try:
        interrupt.bind(loop)
        interrupt._handle(signal.SIGINT, None)
        loop.run_until_complete(asyncio.wait_for(interrupt.wait(), timeout=1))
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

    with interrupt.installed():
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

    with interrupt.installed():
        interrupt._handle(signal.SIGINT, None)
        interrupt._handle(signal.SIGTERM, None)

    assert interrupt.signum == signal.SIGINT
