"""Contract and tree-check tests for the snapshot member inventory."""

from __future__ import annotations

import hashlib
import json
import os
import typing as typ
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


_SAFE_SEGMENT = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True)
#: Nine characters, so a generated segment (at most eight) can never equal it.
_ALLOWANCE_ROOT = "allowance"


def _allowed_directory() -> st.SearchStrategy[str]:
    """Generate an allowance below a root the generated tree cannot name."""
    inner = st.lists(_SAFE_SEGMENT, min_size=1, max_size=2).map("/".join)
    return inner.map(lambda path: f"{_ALLOWANCE_ROOT}/{path}")


def _materialise_allowance(root: Path, allowance: str) -> None:
    """Create allowed content, including a directory below the allowance."""
    for relative in (f"{allowance}/plugin.py", f"{allowance}/nested/module.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"setup")


def _prefix_free_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """Drop paths that would make one file an ancestor of another."""
    kept: dict[str, bytes] = {}
    for path in sorted(files, key=lambda item: (item.count("/"), item)):
        if not any(path.startswith(f"{parent}/") for parent in kept):
            kept[path] = files[path]
    return kept


def _generated_files() -> st.SearchStrategy[dict[str, bytes]]:
    """Generate a prefix-free mapping of safe relative paths to payloads."""
    paths = st.lists(_SAFE_SEGMENT, min_size=1, max_size=3).map("/".join)
    return st.dictionaries(
        keys=paths,
        values=st.binary(max_size=32),
        min_size=1,
        max_size=6,
    ).map(_prefix_free_files)


def _materialise_tree(
    root: Path,
    files: dict[str, bytes],
) -> list[dict[str, object]]:
    """Create the generated files and return their matching inventory entries."""
    directories = {
        "/".join(path.split("/")[:depth])
        for path in files
        for depth in range(1, path.count("/") + 1)
    }
    for directory in sorted(directories):
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o755)
    entries: list[dict[str, object]] = [
        {"path": directory, "kind": KIND_DIRECTORY, "mode": 0o755}
        for directory in sorted(directories)
    ]
    for relative, payload in sorted(files.items()):
        target = root / relative
        target.write_bytes(payload)
        target.chmod(0o644)
        entries.append(_file(relative, payload))
    return entries


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=(
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ),
)
@given(files=_generated_files(), allowance=_allowed_directory())
def test_validate_tree_matches_generated_inventory_and_allowances(
    tmp_path: Path,
    files: dict[str, bytes],
    allowance: str,
) -> None:
    """A generated tree validates, and only exact allowances are exempt."""
    # `tmp_path` is shared across examples, so each input gets its own tree.
    root = tmp_path / f"tree-{uuid.uuid4().hex}"
    root.mkdir()
    inventory = parse_inventory(_document(_materialise_tree(root, files)))

    validate_tree(inventory, root)

    modified = min(files)
    (root / modified).write_bytes(files[modified] + b"!")
    with pytest.raises(ProverToolError):
        validate_tree(inventory, root)
    (root / modified).write_bytes(files[modified])

    removed = max(files)
    payload = (root / removed).read_bytes()
    (root / removed).unlink()
    with pytest.raises(ProverToolError, match="missing"):
        validate_tree(inventory, root)
    (root / removed).write_bytes(payload)
    (root / removed).chmod(0o644)

    (root / "unapproved-extra").write_bytes(b"extra")
    with pytest.raises(ProverToolError, match="not recorded"):
        validate_tree(inventory, root)
    (root / "unapproved-extra").unlink()

    # An allowance exempts its descendants and its implicit parents, but the
    # allowance itself must still be named.
    _materialise_allowance(root, allowance)
    with pytest.raises(ProverToolError, match="not recorded"):
        validate_tree(inventory, root)
    validate_tree(inventory, root, allowed_extra=(allowance,))

    # A sibling subtree that merely shares the allowance's name is not exempt.
    sibling = f"{allowance}-sibling"
    _materialise_allowance(root, sibling)
    with pytest.raises(ProverToolError, match="not recorded"):
        validate_tree(inventory, root, allowed_extra=(allowance,))
