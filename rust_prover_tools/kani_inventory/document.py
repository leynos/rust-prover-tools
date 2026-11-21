"""Read and write the strict version-1 inventory document.

The document is the inspectable interface between discovery and execution. It
is strict on purpose: every key is required, no unknown key is permitted, an
empty package is refused, and a duplicate identity is refused. A lenient
reader would let a hand-edited inventory select fewer proofs than its author
intended, and the resulting run would still look complete.

Examples
--------
>>> document = inventory_document(HarnessInventory((
...     PackageInventory("p", (
...         InventoryHarness("p", "p", "lib", "h::x", "src/x.rs", 1),
...     )),
... )))
>>> document["schema"]
1
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.kani_inventory.models import (
    SCHEMA_VERSION,
    HarnessInventory,
    InventoryFormatError,
    InventoryHarness,
    PackageInventory,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "inventory_document",
    "inventory_from_document",
    "load_inventory",
    "write_inventory",
)


def inventory_document(inventory: HarnessInventory) -> dict[str, object]:
    """Render an inventory as its deterministic version-1 document.

    Parameters
    ----------
    inventory : HarnessInventory
        Inventory to render.

    Returns
    -------
    dict[str, object]
        A JSON-compatible mapping.
    """
    return {
        "schema": SCHEMA_VERSION,
        "packages": [
            {
                "package": package.package,
                "harnesses": [
                    {
                        "package": harness.package,
                        "target": harness.target,
                        "target_kind": harness.target_kind,
                        "harness": harness.harness,
                        "source": harness.source,
                        "line": harness.line,
                        "conditional": harness.conditional,
                        "attributes": list(harness.attributes),
                    }
                    for harness in package.harnesses
                ],
            }
            for package in inventory.packages
        ],
    }


def write_inventory(path: Path, inventory: HarnessInventory) -> None:
    """Write an inventory document, creating its parent directory.

    Parameters
    ----------
    path : Path
        Destination path.
    inventory : HarnessInventory
        Inventory to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(inventory_document(inventory), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def load_inventory(path: Path) -> HarnessInventory:
    """Load and validate a strict version-1 inventory document.

    Parameters
    ----------
    path : Path
        Document to read.

    Returns
    -------
    HarnessInventory
        The validated inventory.

    Raises
    ------
    InventoryFormatError
        Raised for malformed JSON, an unknown schema, a missing or unknown
        key, an empty package, or a duplicate identity.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        msg = f"{path} is not a readable inventory document: {error}"
        raise InventoryFormatError(msg) from error
    return inventory_from_document(document)


def inventory_from_document(document: object) -> HarnessInventory:
    """Validate a parsed inventory document.

    Parameters
    ----------
    document : object
        Parsed JSON value.

    Returns
    -------
    HarnessInventory
        The validated inventory.

    Raises
    ------
    InventoryFormatError
        Raised for any schema mismatch.
    """
    root = _object(document, "the inventory")
    _exact_keys(root, {"schema", "packages"}, "the inventory")
    schema = _integer(root["schema"], "the inventory schema")
    if schema != SCHEMA_VERSION:
        msg = f"the inventory has schema {root['schema']!r}, not {SCHEMA_VERSION}"
        raise InventoryFormatError(msg)
    entries = _array(root["packages"], "the inventory packages")
    packages = tuple(_package(entry) for entry in entries)
    try:
        return HarnessInventory(packages)
    except ValueError as error:
        raise InventoryFormatError(str(error)) from error


def _package(document: object) -> PackageInventory:
    """Validate one package entry."""
    root = _object(document, "a package entry")
    _exact_keys(root, {"package", "harnesses"}, "a package entry")
    name = _string(root["package"], "a package name")
    entries = _array(root["harnesses"], f"package {name!r} harnesses")
    harnesses = tuple(_harness(entry) for entry in entries)
    try:
        return PackageInventory(name, harnesses)
    except ValueError as error:
        raise InventoryFormatError(str(error)) from error


def _harness(document: object) -> InventoryHarness:
    """Validate one harness entry."""
    root = _object(document, "a harness entry")
    _exact_keys(
        root,
        {
            "package",
            "target",
            "target_kind",
            "harness",
            "source",
            "line",
            "conditional",
            "attributes",
        },
        "a harness entry",
    )
    try:
        return InventoryHarness(
            package=_string(root["package"], "a harness package"),
            target=_string(root["target"], "a harness target"),
            target_kind=_string(root["target_kind"], "a harness target kind"),
            harness=_string(root["harness"], "a harness name"),
            source=_string(root["source"], "a harness source"),
            line=_integer(root["line"], "a harness line"),
            conditional=_boolean(root["conditional"], "a harness conditional flag"),
            attributes=tuple(
                _string(item, "a harness attribute")
                for item in _array(root["attributes"], "harness attributes")
            ),
        )
    except ValueError as error:
        raise InventoryFormatError(str(error)) from error


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        msg = f"{subject} is not an object"
        raise InventoryFormatError(msg)
    return typ.cast("dict[str, object]", value)


def _array(value: object, subject: str) -> cabc.Sequence[object]:
    """Require a JSON array."""
    if not isinstance(value, list):
        msg = f"{subject} is not an array"
        raise InventoryFormatError(msg)
    return value


def _string(value: object, subject: str) -> str:
    """Require a non-empty JSON string."""
    if not isinstance(value, str) or not value:
        msg = f"{subject} is not a non-empty string"
        raise InventoryFormatError(msg)
    return value


def _integer(value: object, subject: str) -> int:
    """Require a JSON integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{subject} is not an integer"
        raise InventoryFormatError(msg)
    return value


def _boolean(value: object, subject: str) -> bool:
    """Require a JSON boolean."""
    if not isinstance(value, bool):
        msg = f"{subject} is not a boolean"
        raise InventoryFormatError(msg)
    return value


def _exact_keys(
    document: cabc.Mapping[str, object],
    fields: set[str],
    subject: str,
) -> None:
    """Require exactly `fields`, naming missing and unknown keys."""
    missing = sorted(fields - set(document))
    unknown = sorted(set(document) - fields)
    if missing or unknown:
        msg = f"{subject} has missing={missing} and unknown={unknown}"
        raise InventoryFormatError(msg)
