"""Bounded member inventories for distributed snapshot trees.

The behaviour is split by concern:

- `inventory_model` defines members, kinds and resource ceilings;
- `inventory_parse` enforces the version 1 document contract;
- `inventory_tree` compares an extracted tree with its recorded inventory;
- `inventory_archive` compares validated archive members with the inventory.

Callers use this module's `parse_inventory`, `load_inventory`, `validate_tree`
and `validate_archive_inventory` facade.
"""

from __future__ import annotations

from rust_prover_tools.kani_snapshot.inventory_archive import (
    validate_archive_inventory,
)
from rust_prover_tools.kani_snapshot.inventory_model import (
    DEFAULT_INVENTORY_LIMITS,
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    MAX_INVENTORY_MODE,
    MAX_INVENTORY_PATH_LENGTH,
    InventoryEntry,
    InventoryLimits,
    SnapshotInventory,
)
from rust_prover_tools.kani_snapshot.inventory_parse import (
    load_inventory,
    parse_inventory,
)
from rust_prover_tools.kani_snapshot.inventory_tree import validate_tree

__all__ = (
    "DEFAULT_INVENTORY_LIMITS",
    "KIND_DIRECTORY",
    "KIND_FILE",
    "KIND_HARDLINK",
    "KIND_SYMLINK",
    "MAX_INVENTORY_MODE",
    "MAX_INVENTORY_PATH_LENGTH",
    "InventoryEntry",
    "InventoryLimits",
    "SnapshotInventory",
    "load_inventory",
    "parse_inventory",
    "validate_archive_inventory",
    "validate_tree",
)
