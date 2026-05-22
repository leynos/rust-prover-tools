"""Snapshot tests for stable CLI output."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.cli import app

if typ.TYPE_CHECKING:
    import pytest
    from syrupy.assertion import SnapshotAssertion


def test_top_level_help_snapshot(
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """Top-level help output remains stable."""
    app(["--help"], exit_on_error=False)
    captured = capsys.readouterr()
    assert captured.out == snapshot, "Top-level help output does not match snapshot"
