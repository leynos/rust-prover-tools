"""Shared fixture-workspace helpers for the inventory discovery tests."""

from __future__ import annotations

from pathlib import Path

from rust_prover_tools.kani_inventory import HarnessInventory, discover_inventory

WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


def inventory(*packages: str) -> HarnessInventory:
    """Discover the requested fixture packages."""
    return discover_inventory(WORKSPACE, packages)
