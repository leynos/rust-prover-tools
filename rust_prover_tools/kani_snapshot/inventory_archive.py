"""Structural agreement between an inventory and an archive member list.

The archive's own digest is pinned by the manifest, so comparing every member's
effective path, kind, mode, size and link target proves that extraction will
produce exactly the tree the inventory describes. File digests are checked from
the extracted tree, where the bytes are available.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.inventory_model import (
    DEFAULT_INVENTORY_LIMITS,
    InventoryEntry,
    InventoryLimits,
    SnapshotInventory,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.archive_graph import ArchiveEntry

__all__ = ("validate_archive_inventory",)


def validate_archive_inventory(
    entries: cabc.Sequence[ArchiveEntry],
    inventory: SnapshotInventory,
    *,
    limits: InventoryLimits = DEFAULT_INVENTORY_LIMITS,
) -> None:
    """Require an archive and inventory to describe the same members.

    Parameters
    ----------
    entries : Sequence[ArchiveEntry]
        Validated archive members after path mapping.
    inventory : SnapshotInventory
        Parsed producer inventory.
    limits : InventoryLimits
        Ceilings that must also bound the inventory.

    Raises
    ------
    ProverToolError
        Raised when either side has a member the other lacks, a repeated
        path, or a differing kind, mode, size or link target.
    """
    _check_limits(inventory, limits)
    recorded = {entry.path: entry for entry in inventory.entries}
    if len(recorded) != len(inventory.entries):
        msg = "snapshot inventory repeats a path"
        raise ProverToolError(msg)
    archived = {entry.path: entry for entry in entries}
    if len(archived) != len(entries):
        msg = "snapshot archive repeats a path"
        raise ProverToolError(msg)
    _check_same_paths(recorded, archived)
    for path, wanted in recorded.items():
        _compare(path, archived[path], wanted)


def _check_limits(inventory: SnapshotInventory, limits: InventoryLimits) -> None:
    """Require the inventory itself to stay inside its ceilings."""
    if len(inventory.entries) > limits.max_entries:
        msg = f"snapshot inventory has more than {limits.max_entries} entries"
        raise ProverToolError(msg)
    total = sum(entry.size for entry in inventory.entries)
    if total > limits.max_total_bytes:
        msg = "snapshot inventory declares too many bytes"
        raise ProverToolError(msg)
    for entry in inventory.entries:
        if len(entry.path.encode("utf-8")) > limits.max_path_length:
            msg = f"snapshot inventory path is too long: {entry.path!r}"
            raise ProverToolError(msg)


def _check_same_paths(
    recorded: cabc.Mapping[str, InventoryEntry],
    archived: cabc.Mapping[str, ArchiveEntry],
) -> None:
    """Report a member present on only one side."""
    missing = sorted(set(recorded) - set(archived))
    if missing:
        msg = f"snapshot inventory names a member absent from the archive: {missing[0]}"
        raise ProverToolError(msg)
    extra = sorted(set(archived) - set(recorded))
    if extra:
        msg = f"snapshot archive has a member absent from the inventory: {extra[0]}"
        raise ProverToolError(msg)


def _compare(path: str, archived: ArchiveEntry, wanted: InventoryEntry) -> None:
    """Compare one member's structural fields."""
    fields = (
        ("kind", archived.kind, wanted.kind),
        ("mode", archived.mode, wanted.mode),
        ("size", archived.size, wanted.size),
        ("link target", archived.link_target, wanted.link_target),
    )
    for name, actual, expected in fields:
        if actual != expected:
            msg = (
                f"snapshot archive member '{path}' has a different {name}: "
                f"expected {expected!r}, got {actual!r}"
            )
            raise ProverToolError(msg)
