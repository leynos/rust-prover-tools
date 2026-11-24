"""Unit tests for the consumer smoke's evidence readers.

These helpers read text the CLI and `strace` produce, so they can be tested
without installing a snapshot. The generation-path reader in particular sees a
path the CLI renders unescaped, which may contain spaces.
"""

from __future__ import annotations

from tests.kani_snapshot_smoke_support import generation_from_output


def test_a_generation_path_with_spaces_is_captured_whole() -> None:
    """A cache below `/tmp/kani smoke` yields a path containing a space."""
    path = "/home/tester/kani smoke/.kani-snapshots/abc/generations/gen"
    text = f"snapshot kani-snapshot-2026-11-0d2328a93f0e-r1 installed at {path}\n"

    assert generation_from_output(text) == path


def test_an_ordinary_generation_path_is_captured() -> None:
    """The common path form is unchanged."""
    text = "snapshot tag checked at /cache/abc/generations/gen\n"

    assert generation_from_output(text) == "/cache/abc/generations/gen"


def test_output_without_a_generation_line_yields_none() -> None:
    """A failed install names no generation."""
    assert generation_from_output("error: the payload digest differs\n") is None
