"""Decide which path carries a hard link's bytes, from the archive headers.

An extracted hard-link group is indistinguishable from every one of its names:
each is a regular file sharing one inode. The archive that produced the tree is
the only authority on which name was the link and which carried the bytes, so
the inventory is built against the archive's record and then checked against the
tree. Recording the claim without checking it would describe a link the tree
does not have, and the consumer compares kinds and link targets exactly.

This module owns that record and its checks; `inventory_build` walks the tree
and renders the document.
"""

from __future__ import annotations

import stat
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import ArchivePolicy
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.inventory_model import KIND_FILE, KIND_HARDLINK

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import os
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.inventory_model import InventoryEntry

__all__ = ("archive_carriers", "require_carriers_recorded", "require_shared_inode")


def require_shared_inode(
    relative: str,
    carrier: str,
    status: os.stat_result,
    root: Path,
) -> None:
    """Require an archive's hard-link carrier to be this member's own inode.

    The archive says which name carries the bytes; the extracted tree says
    whether that is true. Recording the archive's claim without checking it
    would describe a link the tree does not have, so the carrier must be a
    regular file under the root and share this member's inode.
    """
    path = root / carrier
    try:
        carrier_status = path.stat(follow_symlinks=False)
    except OSError as exc:
        msg = f"tree member {relative!r} links to a missing carrier: {carrier!r}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(carrier_status.st_mode):
        msg = f"tree member {relative!r} links to a non-file carrier: {carrier!r}"
        raise ProverToolError(msg)
    if (carrier_status.st_dev, carrier_status.st_ino) != (
        status.st_dev,
        status.st_ino,
    ):
        msg = (
            f"tree member {relative!r} does not share an inode with its "
            f"archive carrier {carrier!r}"
        )
        raise ProverToolError(msg)


def require_carriers_recorded(
    entries: cabc.Sequence[InventoryEntry],
    carriers: cabc.Mapping[str, str],
) -> None:
    """Require every archive hard link to appear as this inventory records it."""
    kinds = {entry.path: entry.kind for entry in entries}
    for link, carrier in sorted(carriers.items()):
        if kinds.get(link) != KIND_HARDLINK:
            msg = f"archive hard link {link!r} is not recorded as a hard link"
            raise ProverToolError(msg)
        if kinds.get(carrier) != KIND_FILE:
            msg = (
                f"archive hard link {link!r} names carrier {carrier!r}, which "
                "is not recorded as a file"
            )
            raise ProverToolError(msg)


def archive_carriers(archive: Path, archive_root: str) -> dict[str, str]:
    """Return the archive's hard-link carriers, keyed by extraction path."""
    policy = ArchivePolicy(strip_components=1, required_root=archive_root)
    return {
        entry.path: typ.cast("str", entry.link_target)
        for entry in read_archive(archive, policy=policy)
        if entry.kind == KIND_HARDLINK
    }
