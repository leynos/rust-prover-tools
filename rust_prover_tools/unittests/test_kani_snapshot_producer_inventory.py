"""The producer's inventory is the exact record a consumer validates against.

A generator that quietly skipped a member, followed a symbolic link out of the
tree or recorded a privileged mode would authorise a tree the consumer cannot
actually accept. Each refusal below names such a tree, and the positive case
checks the record round-trips through the consumer's own parser.
"""

from __future__ import annotations

import os
import stat
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
)
from rust_prover_tools.kani_snapshot.inventory_parse import parse_inventory
from rust_prover_tools.kani_snapshot.producer.inventory_build import (
    build_inventory,
    inventory_to_json,
    main,
)

if typ.TYPE_CHECKING:
    import pathlib

    from rust_prover_tools.kani_snapshot.inventory_model import (
        InventoryEntry,
        SnapshotInventory,
    )


def _tree(root: pathlib.Path) -> None:
    """Create a small distributable tree with a file, link and hard link."""
    (root / "bin").mkdir(parents=True)
    executable = root / "bin" / "kani"
    executable.write_bytes(b"launcher")
    executable.chmod(0o755)
    (root / "bin" / "cargo-kani").hardlink_to(executable)
    (root / "lib").mkdir()
    (root / "lib" / "libkani.so").write_bytes(b"library")
    (root / "lib" / "libkani.so.1").symlink_to("libkani.so")


def _by_path(inventory: SnapshotInventory) -> dict[str, InventoryEntry]:
    """Return the inventory's entries keyed by path."""
    return {entry.path: entry for entry in inventory.entries}


def test_the_inventory_records_every_member_in_path_order(
    tmp_path: pathlib.Path,
) -> None:
    """A deterministic walk is the only record the consumer can compare."""
    _tree(tmp_path)

    inventory = build_inventory(tmp_path)

    assert [entry.path for entry in inventory.entries] == [
        "bin",
        "bin/cargo-kani",
        "bin/kani",
        "lib",
        "lib/libkani.so",
        "lib/libkani.so.1",
    ]
    entries = _by_path(inventory)
    assert entries["bin"].kind == KIND_DIRECTORY
    assert entries["bin/cargo-kani"].kind == KIND_FILE
    assert entries["bin/kani"].kind == KIND_HARDLINK
    assert entries["lib/libkani.so.1"].kind == KIND_SYMLINK
    assert entries["lib/libkani.so.1"].link_target == "libkani.so"
    assert entries["bin/cargo-kani"].mode == 0o755


def test_a_hard_link_names_the_first_path_with_the_shared_inode(
    tmp_path: pathlib.Path,
) -> None:
    """Two paths to one inode are recorded once as bytes and once as a link."""
    _tree(tmp_path)

    entries = _by_path(build_inventory(tmp_path))

    file_entry = entries["bin/cargo-kani"]
    link_entry = entries["bin/kani"]
    assert file_entry.kind == KIND_FILE
    assert link_entry.kind == KIND_HARDLINK
    assert link_entry.link_target == "bin/cargo-kani"


def test_the_recorded_digest_is_the_file_digest(tmp_path: pathlib.Path) -> None:
    """The consumer compares bytes, so the record must name those bytes."""
    import hashlib

    _tree(tmp_path)

    entries = _by_path(build_inventory(tmp_path))

    assert entries["lib/libkani.so"].sha256 == hashlib.sha256(b"library").hexdigest()


def test_the_record_round_trips_through_the_consumer_parser(
    tmp_path: pathlib.Path,
) -> None:
    """The producer cannot emit a document its own consumer would refuse."""
    _tree(tmp_path)

    inventory = build_inventory(tmp_path)

    assert parse_inventory(inventory_to_json(inventory)) == inventory


def test_a_symlink_outside_the_tree_is_refused(tmp_path: pathlib.Path) -> None:
    """A link the consumer could follow out of the tree is not distributable."""
    (tmp_path / "escape").symlink_to("/etc/passwd")

    with pytest.raises(ProverToolError, match="links outside the tree"):
        build_inventory(tmp_path)


def test_a_privileged_mode_is_refused(tmp_path: pathlib.Path) -> None:
    """Setuid bits cannot be expressed, so the tree is refused instead."""
    target = tmp_path / "sudo"
    target.write_bytes(b"binary")
    target.chmod(0o4755)

    with pytest.raises(ProverToolError, match="privileged mode"):
        build_inventory(tmp_path)


def test_a_special_file_is_refused(tmp_path: pathlib.Path) -> None:
    """A fifo has no bytes and no recorded kind the consumer accepts."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("fifos are unavailable on this platform")
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(ProverToolError, match="not a file, directory or link"):
        build_inventory(tmp_path)


def test_the_root_must_be_a_directory(tmp_path: pathlib.Path) -> None:
    """A file is not a tree, however plausible its name."""
    target = tmp_path / "bundle.tar.gz"
    target.write_bytes(b"not a tree")

    with pytest.raises(ProverToolError, match="not a directory"):
        build_inventory(target)


def test_the_command_writes_the_inventory(tmp_path: pathlib.Path) -> None:
    """The build job's command writes a document a consumer can parse."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "payload").write_bytes(b"payload")
    output = tmp_path / "inventory.json"

    status = main(["--tree", str(tree), "--output", str(output)])

    assert status == 0
    parsed = parse_inventory(output.read_bytes())
    assert [entry.path for entry in parsed.entries] == ["payload"]
    assert stat.S_IMODE((tree / "payload").stat().st_mode) == parsed.entries[0].mode
