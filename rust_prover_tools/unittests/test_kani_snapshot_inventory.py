"""Contract and tree-check tests for the snapshot member inventory."""

from __future__ import annotations

import hashlib
import json
import os
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.inventory import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    InventoryEntry,
    InventoryLimits,
    SnapshotInventory,
    parse_inventory,
    validate_tree,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_SHA = "a" * 64


def _document(entries: list[dict[str, object]]) -> bytes:
    """Render a version 1 inventory document."""
    return json.dumps(
        {"schema_version": 1, "entries": entries},
        separators=(",", ":"),
    ).encode()


def _file(path: str, data: bytes = b"data\n", mode: int = 0o644) -> dict[str, object]:
    """Return one file entry whose digest matches `data`."""
    return {
        "path": path,
        "kind": KIND_FILE,
        "mode": mode,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _tree(tmp_path: Path) -> tuple[Path, SnapshotInventory, bytes]:
    """Create a small extracted tree and its matching inventory."""
    root = tmp_path / "tree"
    (root / "bin").mkdir(parents=True)
    payload = b"#!/bin/sh\nexit 0\n"
    (root / "bin/tool").write_bytes(payload)
    (root / "bin/tool").chmod(0o755)
    (root / "share").mkdir()
    (root / "share/data.txt").write_text("data\n", encoding="utf-8")
    (root / "share/alias.txt").symlink_to("data.txt")
    (root / "bin/tool-copy").hardlink_to(root / "bin/tool")
    inventory = parse_inventory(
        _document([
            {"path": "bin", "kind": KIND_DIRECTORY, "mode": 0o755},
            {
                "path": "bin/tool",
                "kind": KIND_FILE,
                "mode": 0o755,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            {
                "path": "bin/tool-copy",
                "kind": KIND_HARDLINK,
                "mode": 0o755,
                "target": "bin/tool",
            },
            {"path": "share", "kind": KIND_DIRECTORY, "mode": 0o755},
            {
                "path": "share/data.txt",
                "kind": KIND_FILE,
                "mode": 0o644,
                "size": 5,
                "sha256": hashlib.sha256(b"data\n").hexdigest(),
            },
            {
                "path": "share/alias.txt",
                "kind": KIND_SYMLINK,
                "mode": 0o777,
                "target": "data.txt",
            },
        ])
    )
    return root, inventory, payload


def test_valid_inventory_parses_all_supported_kinds() -> None:
    """Files, directories, symbolic links and hard links are accepted."""
    inventory = parse_inventory(
        _document([
            {"path": "bin", "kind": KIND_DIRECTORY, "mode": 0o755},
            _file("bin/tool", mode=0o755),
            {
                "path": "bin/tool-copy",
                "kind": KIND_HARDLINK,
                "mode": 0o755,
                "target": "bin/tool",
            },
            {
                "path": "bin/alias",
                "kind": KIND_SYMLINK,
                "mode": 0o777,
                "target": "tool",
            },
        ])
    )

    assert inventory.schema_version == 1
    assert [entry.path for entry in inventory.entries] == [
        "bin",
        "bin/tool",
        "bin/tool-copy",
        "bin/alias",
    ]


@pytest.mark.parametrize(
    "document",
    [
        b'{"schema_version":1,"entries":[],"extra":true}',
        b'{"schema_version":1}',
        b'{"schema_version":2,"entries":[]}',
        b'{"schema_version":true,"entries":[]}',
        b'{"schema_version":1,"entries":{}}',
        b'{"schema_version":1,"entries":[{"path":"a","kind":"file","mode":420,'
        b'"size":1,"sha256":"' + _SHA.encode() + b'","extra":1}]}',
        (
            b'{"schema_version":1,"entries":[{"path":"a","kind":"file",'
            b'"mode":420,"size":1}]}'
        ),
        b'{"schema_version":1,"entries":[{"path":"a","kind":"directory",'
        b'"mode":420,"sha256":"' + _SHA.encode() + b'"}]}',
        b'{"schema_version":1,"entries":[{"path":"a","kind":"symlink","mode":511}]}',
        b'{"schema_version":1,"entries":[{"path":"a","kind":"device","mode":420}]}',
        b'{"schema_version":1,"entries":[{"path":"a","kind":"file","mode":420,'
        b'"size":-1,"sha256":"' + _SHA.encode() + b'"}]}',
        b'{"schema_version":1,"entries":[{"path":"a","kind":"file","mode":4755,'
        b'"size":1,"sha256":"' + _SHA.encode() + b'"}]}',
    ],
    ids=[
        "unknown-top-key",
        "missing-entries",
        "wrong-schema",
        "boolean-schema",
        "entries-not-array",
        "unknown-entry-key",
        "file-missing-digest",
        "directory-with-digest",
        "symlink-missing-target",
        "unsupported-kind",
        "negative-size",
        "privileged-mode",
    ],
)
def test_invalid_inventory_documents_are_rejected(document: bytes) -> None:
    """Every structural deviation from version 1 fails closed."""
    with pytest.raises(ProverToolError, match="inventory"):
        parse_inventory(document)


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "a\\b", "a\x00b", "", "."],
)
def test_unsafe_inventory_paths_are_rejected(path: str) -> None:
    """Paths must be normalized and contained before they are compared."""
    with pytest.raises(ProverToolError, match="inventory"):
        parse_inventory(_document([_file(path)]))


def test_duplicate_inventory_paths_are_rejected() -> None:
    """The same effective path may not appear twice."""
    with pytest.raises(ProverToolError, match="more than once"):
        parse_inventory(_document([_file("a"), _file("a")]))


def test_validate_tree_accepts_the_recorded_tree(tmp_path: Path) -> None:
    """A tree matching every recorded attribute validates."""
    root, inventory, _ = _tree(tmp_path)

    validate_tree(inventory, root)


def test_validate_tree_rejects_modified_file_content(tmp_path: Path) -> None:
    """A digest mismatch means the cached tree is corrupt."""
    root, inventory, _ = _tree(tmp_path)
    (root / "bin/tool").write_bytes(b"changed\n")

    with pytest.raises(ProverToolError, match=r"digest|size"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_modified_mode(tmp_path: Path) -> None:
    """Permission drift is corruption, not a cosmetic difference."""
    root, inventory, _ = _tree(tmp_path)
    (root / "bin/tool").chmod(0o700)

    with pytest.raises(ProverToolError, match="mode"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_an_extra_member(tmp_path: Path) -> None:
    """Unexpected content may not hide inside a validated generation."""
    root, inventory, _ = _tree(tmp_path)
    (root / "share/extra.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(ProverToolError, match="not recorded"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_a_missing_member(tmp_path: Path) -> None:
    """A missing recorded member is an incomplete generation."""
    root, inventory, _ = _tree(tmp_path)
    (root / "share/data.txt").unlink()

    with pytest.raises(ProverToolError, match="missing"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_a_changed_symbolic_target(tmp_path: Path) -> None:
    """Links are checked by their exact recorded text."""
    root, inventory, _ = _tree(tmp_path)
    (root / "share/alias.txt").unlink()
    (root / "share/alias.txt").symlink_to("elsewhere")

    with pytest.raises(ProverToolError, match="symbolic"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_a_broken_hard_link(tmp_path: Path) -> None:
    """A recorded hard link must still share its target's inode."""
    root, inventory, _ = _tree(tmp_path)
    (root / "bin/tool-copy").unlink()
    (root / "bin/tool-copy").write_bytes(b"#!/bin/sh\nexit 0\n")
    (root / "bin/tool-copy").chmod(0o755)

    with pytest.raises(ProverToolError, match="hard link"):
        validate_tree(inventory, root)


def test_validate_tree_rejects_a_special_file(tmp_path: Path) -> None:
    """A FIFO is never a valid recorded member."""
    root, inventory, _ = _tree(tmp_path)
    os.mkfifo(root / "share/pipe")

    with pytest.raises(ProverToolError, match=r"special|unexpected"):
        validate_tree(inventory, root)


def test_inventory_limits_bound_entries_and_bytes(tmp_path: Path) -> None:
    """Entry-count and total-size ceilings stop oversized inventories."""
    root, inventory, _ = _tree(tmp_path)

    with pytest.raises(ProverToolError, match="entries"):
        validate_tree(inventory, root, limits=InventoryLimits(max_entries=3))
    with pytest.raises(ProverToolError, match="bytes"):
        validate_tree(inventory, root, limits=InventoryLimits(max_total_bytes=3))


def test_inventory_entry_is_hashable_and_explicit() -> None:
    """The validated entry shape is frozen and self-describing."""
    entry = InventoryEntry(path="a", kind=KIND_FILE, mode=0o644, size=1, sha256=_SHA)

    assert entry == InventoryEntry(
        path="a", kind=KIND_FILE, mode=0o644, size=1, sha256=_SHA
    )
