"""Comparison of an extracted tree with its recorded inventory.

The check is exhaustive in both directions: every recorded member must exist
with the recorded kind, mode, size, digest and link target, and every observed
member must be recorded. Synthetic hard links are verified by inode identity,
not merely by content, so a replaced copy cannot masquerade as the original.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import os
import stat
import typing as typ
from pathlib import Path

from rust_prover_tools.download_stream import READ_CHUNK_SIZE
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.inventory_model import (
    DEFAULT_INVENTORY_LIMITS,
    KIND_DIRECTORY,
    KIND_HARDLINK,
    KIND_SYMLINK,
    InventoryEntry,
    InventoryLimits,
    SnapshotInventory,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("validate_tree",)

_MEMBER_PREVIEW: typ.Final[int] = 5


def validate_tree(
    inventory: SnapshotInventory,
    root: Path,
    *,
    limits: InventoryLimits = DEFAULT_INVENTORY_LIMITS,
    allowed_extra: cabc.Collection[str] = (),
) -> None:
    """Check an extracted tree against its recorded inventory.

    Parameters
    ----------
    inventory : SnapshotInventory
        Expected members.
    root : Path
        Extracted tree to inspect.
    limits : InventoryLimits
        Entry, byte and path ceilings.
    allowed_extra : collections.abc.Collection[str]
        Paths that setup or launcher placement may add on top of the
        inventory, such as the private home's Python root and the toolchain
        link. Their implicit parent directories are allowed too.

    Raises
    ------
    ProverToolError
        Raised for a missing or unrecorded member, a kind, mode, size, digest
        or link-target mismatch, a special file, or a limit breach.
    """
    _check_inventory_limits(inventory, limits)
    scan = _TreeScan(limits=limits)
    _scan_tree(scan, root)
    recorded = {entry.path: entry for entry in inventory.entries}
    missing = sorted(set(recorded) - set(scan.observed))
    if missing:
        msg = f"snapshot tree is missing recorded members: {_preview(missing)}"
        raise ProverToolError(msg)
    extra = sorted(set(scan.observed) - set(recorded) - _allowed_paths(allowed_extra))
    if extra:
        msg = f"snapshot tree contains members not recorded: {_preview(extra)}"
        raise ProverToolError(msg)
    for entry in inventory.entries:
        _check_member(entry, scan.observed[entry.path], root)


def _allowed_paths(allowed_extra: cabc.Collection[str]) -> set[str]:
    """Return the allowances plus every implicit parent of each one."""
    allowed: set[str] = set()
    for path in allowed_extra:
        parts = path.split("/")
        allowed.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    return allowed


def _preview(paths: list[str]) -> str:
    """Render a bounded, deterministic list of member paths."""
    shown = ", ".join(paths[:_MEMBER_PREVIEW])
    if len(paths) > _MEMBER_PREVIEW:
        return f"{shown}, ... ({len(paths)} total)"
    return shown


def _check_inventory_limits(
    inventory: SnapshotInventory, limits: InventoryLimits
) -> None:
    """Reject an inventory larger than the configured ceilings."""
    if len(inventory.entries) > limits.max_entries:
        msg = f"snapshot inventory records more than {limits.max_entries} entries"
        raise ProverToolError(msg)
    total = sum(entry.size for entry in inventory.entries)
    if total > limits.max_total_bytes:
        msg = f"snapshot inventory records more than {limits.max_total_bytes} bytes"
        raise ProverToolError(msg)


@dc.dataclass(slots=True)
class _TreeScan:
    """Mutable work state for one bounded directory walk."""

    limits: InventoryLimits
    observed: dict[str, os.stat_result] = dc.field(default_factory=dict)
    stack: list[tuple[Path, str]] = dc.field(default_factory=list)


def _scan_tree(scan: _TreeScan, root: Path) -> None:
    """Collect lstat results for every member below `root`, without following."""
    try:
        root_stat = root.lstat()
    except OSError as exc:
        msg = f"failed to inspect snapshot tree '{root}': {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        msg = f"snapshot tree root is not a directory: {root}"
        raise ProverToolError(msg)
    scan.stack.append((root, ""))
    while scan.stack:
        directory, prefix = scan.stack.pop()
        _scan_directory(scan, directory, prefix)


def _scan_directory(scan: _TreeScan, directory: Path, prefix: str) -> None:
    """Record one directory's children and queue real subdirectories."""
    try:
        with os.scandir(directory) as children:
            for child in children:
                _scan_child(scan, child, prefix)
    except OSError as exc:
        msg = f"failed to inspect snapshot tree directory '{directory}': {exc}"
        raise ProverToolError(msg) from exc


def _scan_child(scan: _TreeScan, child: os.DirEntry[str], prefix: str) -> None:
    """Record one directory entry, rejecting unsupported member types."""
    relative = f"{prefix}/{child.name}" if prefix else child.name
    if len(relative.encode("utf-8")) > scan.limits.max_path_length:
        msg = f"snapshot tree path exceeds {scan.limits.max_path_length} bytes"
        raise ProverToolError(msg)
    status = child.stat(follow_symlinks=False)
    scan.observed[relative] = status
    if stat.S_ISDIR(status.st_mode):
        scan.stack.append((Path(child.path), relative))
    elif not (stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode)):
        msg = f"snapshot tree contains a special file: '{relative}'"
        raise ProverToolError(msg)


def _check_member(entry: InventoryEntry, status: os.stat_result, root: Path) -> None:
    """Compare one recorded member with its observed filesystem status."""
    _check_mode(entry, status)
    if entry.kind == KIND_DIRECTORY:
        if not stat.S_ISDIR(status.st_mode):
            _fail_kind(entry, "directory")
        return
    if entry.kind == KIND_SYMLINK:
        _check_symlink(entry, status, root)
        return
    if not stat.S_ISREG(status.st_mode):
        _fail_kind(entry, "regular file")
    if entry.kind == KIND_HARDLINK:
        _check_hardlink(entry, status, root)
        return
    if status.st_size != entry.size:
        msg = f"snapshot tree member '{entry.path}' has the wrong size"
        raise ProverToolError(msg)
    if _sha256_file(root / entry.path) != entry.sha256:
        msg = f"snapshot tree member '{entry.path}' has the wrong digest"
        raise ProverToolError(msg)


def _check_mode(entry: InventoryEntry, status: os.stat_result) -> None:
    """Require ordinary permission bits to match exactly."""
    if stat.S_IMODE(status.st_mode) != entry.mode:
        msg = f"snapshot tree member '{entry.path}' has the wrong mode"
        raise ProverToolError(msg)


def _check_symlink(entry: InventoryEntry, status: os.stat_result, root: Path) -> None:
    """Require an exact symbolic-link target."""
    if not stat.S_ISLNK(status.st_mode):
        _fail_kind(entry, "symbolic link")
    try:
        actual = str((root / entry.path).readlink())
    except OSError as exc:
        msg = f"failed to read snapshot symbolic link '{entry.path}': {exc}"
        raise ProverToolError(msg) from exc
    if actual != entry.link_target:
        msg = f"snapshot symbolic link '{entry.path}' has the wrong target"
        raise ProverToolError(msg)


def _check_hardlink(entry: InventoryEntry, status: os.stat_result, root: Path) -> None:
    """Require a hard link to share its recorded target's inode."""
    target = entry.link_target or ""
    try:
        target_status = (root / target).lstat()
    except OSError as exc:
        msg = f"snapshot hard link '{entry.path}' target is missing: {exc}"
        raise ProverToolError(msg) from exc
    if (status.st_dev, status.st_ino) != (target_status.st_dev, target_status.st_ino):
        msg = f"snapshot hard link '{entry.path}' does not match its target"
        raise ProverToolError(msg)


def _fail_kind(entry: InventoryEntry, expected: str) -> typ.NoReturn:
    """Raise a kind mismatch naming the recorded path."""
    msg = f"snapshot tree member '{entry.path}' is not a {expected}"
    raise ProverToolError(msg)


def _sha256_file(path: Path) -> str:
    """Return the digest of a file, reporting read failures."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(READ_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        msg = f"failed to read snapshot tree member '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return digest.hexdigest()
