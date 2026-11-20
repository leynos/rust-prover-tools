"""Build the version 1 inventory of a distributable payload tree.

The build job has the tree; the consumer has only the record. This module turns
the first into the second, once, deterministically: members are walked in sorted
order, every regular file is hashed from its exact bytes, and a hard link is
recorded by the first path that carried the shared inode. A tree the inventory
cannot express — a special file, a privileged mode bit, an absolute or escaping
symbolic link — is refused rather than described approximately, because the
consumer validates the installed tree against this document and an approximate
record would authorise an approximate tree.

The rendering mirrors `inventory_parse`, so a document written here is one a
consumer can read without a second schema.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import os
import pathlib
import stat
import sys
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    MAX_INVENTORY_MODE,
    InventoryEntry,
    SnapshotInventory,
)
from rust_prover_tools.kani_snapshot.inventory_parse import normalize_inventory_path
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.orchestrator.documents import write_document
from rust_prover_tools.kani_snapshot.orchestrator.inventory_carriers import (
    archive_carriers,
    require_carriers_recorded,
    require_shared_inode,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("build_inventory", "inventory_to_json", "main")

#: Ceiling on members, matching the consumer's default inventory limit.
_MAX_ENTRIES: typ.Final[int] = 100_000


@dc.dataclass(slots=True)
class _Walk:
    """State shared by every step of one inventory walk.

    Attributes
    ----------
    root : pathlib.Path
        Resolved inventory root; a link destination must stay beneath it.
    hardlinks : dict[tuple[int, int], str]
        First recorded path for each inode that is linked more than once. Only
        used when the archive that produced the tree is not supplied.
    carriers : collections.abc.Mapping of str to str or None
        The archive's own record of which path carries the bytes for each hard
        link. An extracted tree cannot recover that distinction: every name in
        a hard-link group is the same inode, so the walk order alone decides
        which name it calls the file. The archive headers do record it, and the
        consumer compares member kinds exactly, so when they are available they
        decide. `None` keeps the walk-order designation.
    """

    root: pathlib.Path
    hardlinks: dict[tuple[int, int], str]
    carriers: cabc.Mapping[str, str] | None = None


def _relative(prefix: str, name: str) -> str:
    """Return the tree-relative path of one member."""
    return f"{prefix}{name}"


def _mode(relative: str, status: os.stat_result) -> int:
    """Return ordinary permission bits, refusing privileged ones."""
    mode = stat.S_IMODE(status.st_mode)
    if mode > MAX_INVENTORY_MODE:
        msg = f"tree member {relative!r} carries privileged mode bits: {mode:o}"
        raise ProverToolError(msg)
    return mode


def _link_target(relative: str, target: str) -> str:
    """Require a symbolic link target a consumer can follow inside the tree."""
    if not target:
        msg = f"tree member {relative!r} is a symbolic link with an empty target"
        raise ProverToolError(msg)
    if "\x00" in target or "\\" in target:
        msg = f"tree member {relative!r} links outside the tree: {target!r}"
        raise ProverToolError(msg)
    if target.startswith("/"):
        msg = f"tree member {relative!r} links outside the tree: {target!r}"
        raise ProverToolError(msg)
    return target


def _require_contained_link(
    relative: str,
    target: str,
    link_path: pathlib.Path,
    root: pathlib.Path,
) -> None:
    """Require a link's resolved destination to stay inside the tree.

    Link text alone is not enough: `..` components and chains through other
    links can leave the root while every individual target looks relative.
    Resolving follows the chain, so the check covers both cases.
    """
    try:
        destination = (link_path.parent / target).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        msg = f"tree member {relative!r} has an unresolvable link: {target!r}"
        raise ProverToolError(msg) from exc
    if not destination.is_relative_to(root):
        msg = f"tree member {relative!r} links outside the tree: {target!r}"
        raise ProverToolError(msg)


def _hardlink_target(
    relative: str,
    status: os.stat_result,
    walk: _Walk,
) -> str | None:
    """Return the path that carries this member's bytes, when it is a link.

    With the archive supplied, its headers decide: a path the archive records
    as a hard link is one, whatever order the walk visits the group in, and a
    path it records as a file stays a file even when the walk meets it second.
    Without it, the first path of each multiply-linked inode carries the bytes.
    """
    if walk.carriers is not None:
        carrier = walk.carriers.get(relative)
        if carrier is None:
            return None
        require_shared_inode(relative, carrier, status, walk.root)
        return _member_path(carrier)
    identity = (status.st_dev, status.st_ino)
    if status.st_nlink > 1 and identity in walk.hardlinks:
        return _member_path(walk.hardlinks[identity])
    if status.st_nlink > 1:
        walk.hardlinks[identity] = relative
    return None


def _member_path(relative: str) -> str:
    """Require a recorded path the inventory consumer can parse."""
    try:
        return normalize_inventory_path(relative)
    except ProverToolError as exc:
        msg = f"tree member {relative!r} has a name the inventory cannot express: {exc}"
        raise ProverToolError(msg) from exc


def _member(
    entry: os.DirEntry[str],
    relative: str,
    status: os.stat_result,
    walk: _Walk,
) -> InventoryEntry | None:
    """Record one member, or return None after recursing into a directory."""
    relative = _member_path(relative)
    mode = _mode(relative, status)
    if entry.is_symlink():
        path = pathlib.Path(entry.path)
        # `Path.readlink` normalizes the text it returns, dropping a leading
        # "./" that the consumer compares exactly against the archive header.
        target = _link_target(
            relative,
            # `Path` drops the leading "./" this comparison depends on.
            os.readlink(entry.path),  # ruff: ignore[os-readlink]
        )
        _require_contained_link(relative, target, path, walk.root)
        return InventoryEntry(
            path=relative,
            kind=KIND_SYMLINK,
            mode=mode,
            link_target=target,
        )
    if entry.is_dir(follow_symlinks=False):
        return InventoryEntry(path=relative, kind=KIND_DIRECTORY, mode=mode)
    if not entry.is_file(follow_symlinks=False):
        msg = f"tree member {relative!r} is not a file, directory or link"
        raise ProverToolError(msg)
    carrier = _hardlink_target(relative, status, walk)
    if carrier is not None:
        return InventoryEntry(
            path=relative,
            kind=KIND_HARDLINK,
            mode=mode,
            link_target=carrier,
        )
    path = pathlib.Path(entry.path)
    return InventoryEntry(
        path=relative,
        kind=KIND_FILE,
        mode=mode,
        size=status.st_size,
        sha256=sha256_file(path),
    )


def _scan(
    directory: Path,
    prefix: str,
    entries: list[InventoryEntry],
    walk: _Walk,
) -> None:
    """Record every member beneath `directory` in sorted order."""
    with os.scandir(directory) as listing:
        members = sorted(listing, key=lambda member: member.name)
    for entry in members:
        relative = _relative(prefix, entry.name)
        status = entry.stat(follow_symlinks=False)
        recorded = _member(entry, relative, status, walk)
        if recorded is None:
            continue
        entries.append(recorded)
        if len(entries) > _MAX_ENTRIES:
            msg = f"tree has more than {_MAX_ENTRIES} members"
            raise ProverToolError(msg)
        if recorded.kind == KIND_DIRECTORY:
            _scan(pathlib.Path(entry.path), f"{relative}/", entries, walk)


def build_inventory(
    root: Path,
    *,
    carriers: cabc.Mapping[str, str] | None = None,
) -> SnapshotInventory:
    """Walk `root` and return the version 1 inventory of its members.

    Parameters
    ----------
    root : Path
        Distributable tree to describe. The root itself is not recorded.
    carriers : collections.abc.Mapping of str to str or None
        For a tree extracted from an archive, the archive's record of which
        path carries the bytes for each hard link. The consumer compares
        member kinds and link targets exactly, and an extracted hard-link
        group looks identical from every name, so walk order alone can name a
        different carrier than the archive did. `None` uses walk order, which
        is the only designation a bare tree offers.

    Returns
    -------
    SnapshotInventory
        Members in deterministic, depth-first path order.

    Raises
    ------
    ProverToolError
        Raised when the root is not a directory, a member cannot be hashed, a
        special file or privileged mode appears, a symbolic link escapes the
        tree, or a recorded hard link disagrees with the tree it describes.
    """
    if not root.is_dir():
        msg = f"inventory tree {root} is not a directory"
        raise ProverToolError(msg)
    entries: list[InventoryEntry] = []
    _scan(root, "", entries, _Walk(root.resolve(), {}, carriers))
    if carriers is not None:
        require_carriers_recorded(entries, carriers)
    return SnapshotInventory(schema_version=SCHEMA_VERSION, entries=tuple(entries))


def _entry_document(entry: InventoryEntry) -> dict[str, object]:
    """Render one member in the manifest parser's shape."""
    document: dict[str, object] = {
        "path": entry.path,
        "kind": entry.kind,
        "mode": entry.mode,
    }
    if entry.kind == KIND_FILE:
        document["size"] = entry.size
        document["sha256"] = entry.sha256
    elif entry.kind in {KIND_SYMLINK, KIND_HARDLINK}:
        document["target"] = entry.link_target
    return document


def inventory_to_json(inventory: SnapshotInventory) -> bytes:
    """Render an inventory as deterministic JSON bytes."""
    document = {
        "schema_version": inventory.schema_version,
        "entries": [_entry_document(entry) for entry in inventory.entries],
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def build_parser() -> argparse.ArgumentParser:
    """Return the inventory command line parser."""
    parser = argparse.ArgumentParser(
        prog="kani-snapshot-inventory",
        description="Describe a distributable tree as a version 1 inventory.",
    )
    parser.add_argument("--tree", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--archive",
        help=(
            "Tarball the tree was extracted from. Its headers decide which "
            "name carries a hard link's bytes, because the extracted inode "
            "cannot."
        ),
    )
    parser.add_argument(
        "--archive-root",
        help=(
            "Single root directory the archive's members share, stripped from "
            "each path. Requires --archive."
        ),
    )
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write the inventory of one tree."""
    parser = build_parser()
    namespace = parser.parse_args(argv)
    if (namespace.archive is None) != (namespace.archive_root is None):
        parser.error("--archive and --archive-root must be given together")
    try:
        carriers = (
            None
            if namespace.archive is None
            else archive_carriers(
                pathlib.Path(namespace.archive), namespace.archive_root
            )
        )
        inventory = build_inventory(pathlib.Path(namespace.tree), carriers=carriers)
        write_document(pathlib.Path(namespace.output), inventory_to_json(inventory))
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-inventory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
