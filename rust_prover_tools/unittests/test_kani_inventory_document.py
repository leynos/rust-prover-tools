"""Hold the strict inventory document and its model to fail-closed behaviour."""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryFormatError,
    InventoryHarness,
    PackageInventory,
    inventory_document,
    inventory_from_document,
    load_inventory,
    write_inventory,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _one_package() -> HarnessInventory:
    """Return a small inventory for document tests."""
    return HarnessInventory((
        PackageInventory(
            "p",
            (
                InventoryHarness(
                    package="p",
                    target="p",
                    target_kind="lib",
                    harness="harnesses::x",
                    source="src/x.rs",
                    line=3,
                    conditional=False,
                    attributes=("kani::proof",),
                ),
            ),
        ),
    ))


def test_the_document_round_trips() -> None:
    """A written inventory reads back to the same value."""
    document = inventory_document(_one_package())

    assert inventory_from_document(document) == _one_package()


def test_an_unknown_key_is_refused() -> None:
    """A document that is not exactly the schema is refused."""
    document = inventory_document(_one_package())
    document["surprise"] = 1

    with pytest.raises(InventoryFormatError, match="unknown"):
        inventory_from_document(document)


def test_a_missing_key_is_refused() -> None:
    """Every documented key is required."""
    document = inventory_document(_one_package())
    del document["packages"]

    with pytest.raises(InventoryFormatError, match="missing"):
        inventory_from_document(document)


@pytest.mark.parametrize("schema", [0, 2, True, 1.0, "1", None])
def test_an_unknown_schema_version_is_refused(schema: object) -> None:
    """The reader names the version it accepts."""
    document = inventory_document(_one_package())
    document["schema"] = schema

    with pytest.raises(InventoryFormatError, match="schema"):
        inventory_from_document(document)


def test_an_empty_package_in_the_document_is_refused() -> None:
    """An empty package array cannot stand in for an inventory."""
    document = inventory_document(_one_package())
    packages = typ.cast("list[dict[str, object]]", document["packages"])
    packages[0]["harnesses"] = []

    with pytest.raises(InventoryFormatError, match="no harnesses"):
        inventory_from_document(document)


def test_a_duplicate_identity_in_the_document_is_refused() -> None:
    """A duplicated identity would double-count accounting."""
    document = inventory_document(_one_package())
    packages = typ.cast("list[dict[str, object]]", document["packages"])
    entries = typ.cast("list[dict[str, object]]", packages[0]["harnesses"])
    entries.append(dict(entries[0]))

    with pytest.raises(InventoryFormatError, match=r"twice|declares"):
        inventory_from_document(document)


def test_writing_and_loading_an_inventory(tmp_path: Path) -> None:
    """The file interface round-trips through the strict reader."""
    path = tmp_path / "nested" / "inventory.json"

    write_inventory(path, _one_package())

    assert load_inventory(path) == _one_package()
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_an_unreadable_inventory_file_is_refused(tmp_path: Path) -> None:
    """A missing or malformed file names the path and the cause."""
    with pytest.raises(InventoryFormatError, match="not a readable"):
        load_inventory(tmp_path / "missing.json")


def test_the_document_is_deterministic() -> None:
    """Two renders of the same inventory are byte-identical."""
    first = json.dumps(inventory_document(_one_package()))
    second = json.dumps(inventory_document(_one_package()))

    assert first == second


def test_an_empty_inventory_model_is_refused() -> None:
    """An inventory with no packages cannot describe a run."""
    with pytest.raises(ValueError, match="no packages"):
        HarnessInventory(())


def test_a_package_inventory_rejects_a_foreign_harness() -> None:
    """A harness must belong to the package that lists it."""
    harness = InventoryHarness(
        package="other",
        target="other",
        target_kind="lib",
        harness="h::x",
        source="src/x.rs",
        line=1,
    )

    with pytest.raises(ValueError, match="belongs to"):
        PackageInventory("p", (harness,))


def test_an_inventory_harness_refuses_an_unknown_target_kind() -> None:
    """The target kind is a closed set."""
    with pytest.raises(ValueError, match="unknown target kind"):
        InventoryHarness(
            package="p",
            target="p",
            target_kind="example",
            harness="h::x",
            source="src/x.rs",
            line=1,
        )


def test_a_harness_converts_to_an_execution_spec() -> None:
    """The inventory identity is the supervision identity."""
    harness = _one_package().harnesses[0]

    assert harness.to_spec().qualified == "p::harnesses::x"
    assert _one_package().specs()[0].harness == "harnesses::x"
