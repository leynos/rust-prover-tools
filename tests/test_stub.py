"""Tests for the generated package stub."""

from __future__ import annotations

import rust_prover_tools


def test_hello_returns_stub_greeting() -> None:
    """The generated package exposes a working greeting."""
    assert rust_prover_tools.hello() == "hello from Python"
