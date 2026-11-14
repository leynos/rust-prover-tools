"""Strict parsing of version 1 snapshot inventory documents.

Every key is checked against the exact shape its member kind permits, so an
unknown field cannot be smuggled past a consumer that only reads known ones.
Paths are normalized with the same grammar as archive members before they are
recorded, and a repeated effective path is refused.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import normalize_member_path
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    MAX_INVENTORY_MODE,
    MAX_INVENTORY_PATH_LENGTH,
    InventoryEntry,
    SnapshotInventory,
)
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_non_negative_int,
    require_sequence,
    require_sha256,
    require_text,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("load_inventory", "parse_inventory")

INVENTORY_KEYS: typ.Final[frozenset[str]] = frozenset({"schema_version", "entries"})
_COMMON_KEYS: typ.Final[frozenset[str]] = frozenset({"path", "kind", "mode"})
_KIND_KEYS: typ.Final[dict[str, frozenset[str]]] = {
    KIND_FILE: _COMMON_KEYS | {"size", "sha256"},
    KIND_DIRECTORY: _COMMON_KEYS,
    KIND_SYMLINK: _COMMON_KEYS | {"target"},
    KIND_HARDLINK: _COMMON_KEYS | {"target"},
}


def parse_inventory(data: bytes) -> SnapshotInventory:
    """Parse exact inventory bytes into a validated model.

    Parameters
    ----------
    data : bytes
        Exact `inventory.json` bytes.

    Returns
    -------
    SnapshotInventory
        The validated inventory.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, wrong types,
        unsupported kinds, unsafe paths or a repeated path.
    """
    document = require_mapping(
        strict_json_loads(data, "inventory"),
        "inventory document",
    )
    reject_unknown_keys(document, INVENTORY_KEYS, "inventory")
    missing = sorted(INVENTORY_KEYS - set(document))
    if missing:
        msg = f"snapshot inventory is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    version = require_int(document["schema_version"], "inventory schema_version")
    if version != SCHEMA_VERSION:
        msg = f"snapshot inventory schema_version must be {SCHEMA_VERSION}"
        raise ProverToolError(msg)
    raw_entries = require_sequence(document["entries"], "inventory entries")
    entries: list[InventoryEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        entry = _parse_entry(raw, index)
        if entry.path in seen:
            msg = f"snapshot inventory lists '{entry.path}' more than once"
            raise ProverToolError(msg)
        seen.add(entry.path)
        entries.append(entry)
    return SnapshotInventory(schema_version=version, entries=tuple(entries))


def _parse_entry(raw: object, index: int) -> InventoryEntry:
    """Parse one member object after checking its kind-specific key set."""
    field = f"inventory entries[{index}]"
    entry = require_mapping(raw, field)
    kind = require_text(entry.get("kind"), f"{field} kind")
    allowed = _KIND_KEYS.get(kind)
    if allowed is None:
        msg = f"snapshot {field} has an unsupported kind: '{kind}'"
        raise ProverToolError(msg)
    reject_unknown_keys(entry, allowed, field)
    missing = sorted(allowed - set(entry))
    if missing:
        msg = f"snapshot {field} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    path = _entry_path(entry, field)
    mode = require_non_negative_int(entry["mode"], f"{field} mode")
    if mode > MAX_INVENTORY_MODE:
        msg = f"snapshot {field} mode has privileged bits: {mode:o}"
        raise ProverToolError(msg)
    return InventoryEntry(
        path=path,
        kind=kind,
        mode=mode,
        size=_entry_size(entry, kind, field),
        sha256=_entry_digest(entry, kind, field),
        link_target=_entry_target(entry, kind, field),
    )


def _entry_path(entry: dict[str, object], field: str) -> str:
    """Normalize a recorded path, reporting archive-grammar failures."""
    try:
        return normalize_member_path(
            require_text(entry["path"], f"{field} path"),
            max_length=MAX_INVENTORY_PATH_LENGTH,
        )
    except ProverToolError as exc:
        msg = f"snapshot {field} path is invalid: {exc}"
        raise ProverToolError(msg) from exc


def _entry_size(entry: dict[str, object], kind: str, field: str) -> int:
    """Return a recorded file size, or zero for a non-file."""
    if kind != KIND_FILE:
        return 0
    return require_non_negative_int(entry["size"], f"{field} size")


def _entry_digest(entry: dict[str, object], kind: str, field: str) -> str | None:
    """Return a recorded file digest, or None for a non-file."""
    if kind != KIND_FILE:
        return None
    return require_sha256(entry["sha256"], f"{field} sha256")


def _entry_target(entry: dict[str, object], kind: str, field: str) -> str | None:
    """Return a recorded link target, normalizing a hard link's path."""
    if kind not in {KIND_SYMLINK, KIND_HARDLINK}:
        return None
    target = require_text(entry["target"], f"{field} target")
    if not target:
        msg = f"snapshot {field} target is empty"
        raise ProverToolError(msg)
    if not _is_contained_target(target):
        msg = f"snapshot {field} target is not a contained path: {target!r}"
        raise ProverToolError(msg)
    if kind == KIND_SYMLINK:
        return target
    try:
        return normalize_member_path(target, max_length=MAX_INVENTORY_PATH_LENGTH)
    except ProverToolError as exc:
        msg = f"snapshot {field} hard link target is invalid: {target!r}: {exc}"
        raise ProverToolError(msg) from exc


def _is_contained_target(target: str) -> bool:
    """Return whether link text is relative and free of hostile characters."""
    return "\x00" not in target and "\\" not in target and not target.startswith("/")


def load_inventory(path: Path) -> SnapshotInventory:
    """Read and parse an inventory file.

    Parameters
    ----------
    path : Path
        Inventory file to read.

    Returns
    -------
    SnapshotInventory
        The validated inventory.

    Raises
    ------
    ProverToolError
        Raised when the file cannot be read or fails `parse_inventory`.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"failed to read snapshot inventory '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return parse_inventory(data)
