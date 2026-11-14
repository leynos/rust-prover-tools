"""Typed members and ceilings for snapshot inventories.

`inventory.json` records every distributable path, kind, mode, size, digest and
link target. The manifest hashes the inventory as a payload; the inventory does
not hash itself. A consumer uses it to prove that a cached generation is still
the tree the producer described, not merely that a directory exists.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError

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
)

KIND_FILE: typ.Final[str] = "file"
KIND_DIRECTORY: typ.Final[str] = "directory"
KIND_SYMLINK: typ.Final[str] = "symlink"
KIND_HARDLINK: typ.Final[str] = "hardlink"

#: Matches the archive path ceiling so a tree cannot grow a name the archive
#: reader would have rejected.
MAX_INVENTORY_PATH_LENGTH: typ.Final[int] = 4096

#: Ordinary permission bits; setuid, setgid and sticky bits are never recorded.
MAX_INVENTORY_MODE: typ.Final[int] = 0o777


@dc.dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One recorded tree member.

    Attributes
    ----------
    path : str
        Normalized tree-relative path.
    kind : str
        One of `KIND_FILE`, `KIND_DIRECTORY`, `KIND_SYMLINK` or `KIND_HARDLINK`.
    mode : int
        Ordinary permission bits without setuid, setgid or sticky bits.
    size : int
        Recorded file size, zero for links and directories.
    sha256 : str | None
        Lowercase digest for a regular file.
    link_target : str | None
        Recorded symbolic or tree-relative hard-link target.
    """

    path: str
    kind: str
    mode: int
    size: int = 0
    sha256: str | None = None
    link_target: str | None = None


@dc.dataclass(frozen=True, slots=True)
class SnapshotInventory:
    """A validated version 1 inventory.

    Attributes
    ----------
    schema_version : int
        Inventory schema version. Only version 1 is accepted.
    entries : tuple[InventoryEntry, ...]
        Members in document order.
    """

    schema_version: int
    entries: tuple[InventoryEntry, ...]


@dc.dataclass(frozen=True, slots=True)
class InventoryLimits:
    """Ceilings applied while checking an extracted tree.

    Attributes
    ----------
    max_entries : int
        Maximum recorded members. Defaults to 100,000.
    max_total_bytes : int
        Maximum summed file sizes. Defaults to 8 GiB.
    max_path_length : int
        Maximum UTF-8 path length in bytes. Defaults to 4 KiB.
    """

    max_entries: int = 100_000
    max_total_bytes: int = 8 * 1024**3
    max_path_length: int = MAX_INVENTORY_PATH_LENGTH

    def __post_init__(self) -> None:
        """Reject a limit that cannot bound the work."""
        if min(self.max_entries, self.max_total_bytes, self.max_path_length) < 1:
            msg = "inventory limits must be positive"
            raise ProverToolError(msg)


DEFAULT_INVENTORY_LIMITS: typ.Final[InventoryLimits] = InventoryLimits()
