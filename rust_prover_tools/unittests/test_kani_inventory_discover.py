"""Hold inventory discovery and its strict document to fail-closed behaviour."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryFormatError,
    InventoryHarness,
    PackageInventory,
    discover_inventory,
    inventory_document,
    inventory_from_document,
    load_inventory,
    write_inventory,
)

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)

_WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


def _inventory(*packages: str) -> HarnessInventory:
    """Discover the requested fixture packages."""
    return discover_inventory(_WORKSPACE, packages)


def test_two_packages_produce_their_own_qualified_identities() -> None:
    """Two consumers can select different packages from one workspace."""
    inventory = _inventory("alpha", "beta")

    assert [package.package for package in inventory.packages] == ["alpha", "beta"]
    assert "alpha::harnesses::verified" in inventory.identities()
    assert "beta::in_main" in inventory.identities()


def test_every_target_kind_is_discovered() -> None:
    """Lib, bin, and test targets each contribute their proofs."""
    inventory = _inventory("beta")

    by_target = {
        harness.target_kind: harness.harness for harness in inventory.harnesses
    }
    assert by_target["bin"] in {"in_main", "helper::in_helper"}
    kinds = {harness.target_kind for harness in inventory.harnesses}
    assert kinds == {"bin", "test"}


def test_a_conditional_proof_is_listed_and_marked() -> None:
    """Discovery records gated proofs rather than guessing whether they exist."""
    inventory = _inventory("alpha")
    conditional = [harness for harness in inventory.harnesses if harness.conditional]

    assert [harness.harness for harness in conditional] == [
        "harnesses::conditional_extra"
    ]


def test_an_unknown_package_is_refused() -> None:
    """A selection naming a package the workspace lacks fails closed."""
    with pytest.raises(InventoryDiscoveryError, match="does not contain"):
        _inventory("missing")


def test_a_duplicate_package_selection_is_refused() -> None:
    """Naming a package twice would double its identities."""
    with pytest.raises(InventoryDiscoveryError, match="twice"):
        _inventory("alpha", "alpha")


def test_an_empty_package_inventory_is_refused() -> None:
    """A package with no proofs is a selection defect, not an empty run."""
    with pytest.raises(InventoryDiscoveryError, match="resolves to no harnesses"):
        _inventory("empty")


def test_a_package_without_a_verifiable_target_is_refused() -> None:
    """Only lib, bin, and test targets can carry harnesses."""
    with pytest.raises(InventoryDiscoveryError, match="no lib, bin, or test"):
        _inventory("no_target")


def test_duplicate_pretty_names_across_targets_are_refused() -> None:
    """Two targets declaring one pretty name cannot be selected exactly."""
    with pytest.raises(InventoryDiscoveryError, match="more than one target"):
        _inventory("duplicate")


def test_an_ambiguous_module_file_is_refused_by_discovery() -> None:
    """Discovery surfaces the scanner's refusal rather than a partial list."""
    with pytest.raises(InventoryDiscoveryError, match="ambiguous"):
        _inventory("ambiguous")


def _metadata_document(*packages: str) -> str:
    """Render the cargo metadata document for selected fixture packages."""
    entries = {
        "alpha": {
            "name": "alpha",
            "manifest_path": str(_WORKSPACE / "alpha" / "Cargo.toml"),
            "targets": [
                {
                    "name": "alpha",
                    "kind": ["lib"],
                    "src_path": str(_WORKSPACE / "alpha" / "src" / "lib.rs"),
                }
            ],
        },
        "beta": {
            "name": "beta",
            "manifest_path": str(_WORKSPACE / "beta" / "Cargo.toml"),
            "targets": [
                {
                    "name": "beta",
                    "kind": ["bin"],
                    "src_path": str(_WORKSPACE / "beta" / "src" / "main.rs"),
                },
                {
                    "name": "integration",
                    "kind": ["test"],
                    "src_path": str(_WORKSPACE / "beta" / "tests" / "integration.rs"),
                },
            ],
        },
    }
    return json.dumps({"packages": [entries[package] for package in packages]})


def test_discovery_reads_cargo_metadata(cmd_mox: CmdMox) -> None:
    """Discovery uses `cargo metadata --no-deps --format-version 1`."""
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stdout=_metadata_document("alpha"))

    inventory = discover_inventory(_WORKSPACE, ("alpha",))

    assert "alpha::harnesses::verified" in inventory.identities()


def test_an_rlib_library_target_is_verifiable(cmd_mox: CmdMox) -> None:
    """Cargo reports a crate-type rlib library as an rlib target."""
    document = {
        "packages": [
            {
                "name": "alpha",
                "manifest_path": str(_WORKSPACE / "alpha" / "Cargo.toml"),
                "targets": [
                    {
                        "name": "alpha",
                        "kind": ["rlib"],
                        "src_path": str(_WORKSPACE / "alpha" / "src" / "lib.rs"),
                    }
                ],
            }
        ]
    }
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stdout=json.dumps(document))

    inventory = discover_inventory(_WORKSPACE, ("alpha",))

    assert "alpha::harnesses::verified" in inventory.identities()


def test_a_failed_metadata_call_is_refused(cmd_mox: CmdMox) -> None:
    """A Cargo failure names the status and the diagnostic."""
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stderr="manifest missing\n", exit_code=101)

    with pytest.raises(InventoryDiscoveryError, match="status 101"):
        discover_inventory(_WORKSPACE, ("alpha",))


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


def test_metadata_can_be_injected_for_tests() -> None:
    """A caller may supply pre-read metadata instead of running Cargo."""
    from rust_prover_tools.kani_inventory.discover import (
        CargoPackage,
        CargoTarget,
    )

    metadata = (
        CargoPackage(
            name="alpha",
            manifest_path=_WORKSPACE / "alpha" / "Cargo.toml",
            targets=(
                CargoTarget(
                    name="alpha",
                    kind="lib",
                    src_path=_WORKSPACE / "alpha" / "src" / "lib.rs",
                ),
            ),
        ),
    )
    inventory = discover_inventory(_WORKSPACE, ("alpha",), metadata=metadata)

    assert "alpha::harnesses::verified" in inventory.identities()


def _live_beta() -> tuple[HarnessInventory, tuple[typ.Any, ...]]:
    """Return the live inventory and metadata for the beta fixture package."""
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    metadata = cargo_metadata(_WORKSPACE)
    inventory = discover_inventory(_WORKSPACE, ("beta",), metadata=metadata)
    return inventory, metadata


def _beta_integration() -> InventoryHarness:
    """Return the beta integration-test harness the live tree declares."""
    return next(
        harness
        for harness in _live_beta()[0].harnesses
        if harness.identity == "beta::integration_proof"
    )


def test_a_live_test_target_is_attributed_exactly() -> None:
    """The pinned entry matches the live target, so the run may proceed."""
    from rust_prover_tools.kani_inventory import verify_test_target_attribution

    live, metadata = _live_beta()

    verify_test_target_attribution((_beta_integration(),), live, metadata)


def test_a_pinned_test_harness_absent_from_the_live_tree_is_refused() -> None:
    """A removed harness must not be credited to a stale inventory entry."""
    from rust_prover_tools.kani_inventory import verify_test_target_attribution

    live, metadata = _live_beta()
    stale = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="integration_proof_removed",
        source="tests/integration.rs",
        line=4,
    )

    with pytest.raises(InventoryDiscoveryError, match="no such harness"):
        verify_test_target_attribution((stale,), live, metadata)


def test_a_pinned_test_harness_that_moved_to_the_library_is_refused() -> None:
    """A test-target entry that now names a lib harness is a stale claim."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
        PackageInventory,
        verify_test_target_attribution,
    )
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    moved = InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness="harnesses::verified",
        source="src/harnesses.rs",
        line=5,
    )
    live = HarnessInventory((PackageInventory("alpha", (moved,)),))
    metadata = cargo_metadata(_WORKSPACE)
    stale = InventoryHarness(
        package="alpha",
        target="integration",
        target_kind="test",
        harness="harnesses::verified",
        source="tests/integration.rs",
        line=5,
    )

    with pytest.raises(InventoryDiscoveryError, match="cannot be attributed"):
        verify_test_target_attribution((stale,), live, metadata)


def test_a_pinned_test_harness_with_a_renamed_target_is_refused() -> None:
    """A target rename changes the attribution claim and is refused."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
        PackageInventory,
        verify_test_target_attribution,
    )
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    same_name = InventoryHarness(
        package="beta",
        target="renamed",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )
    live = HarnessInventory((PackageInventory("beta", (same_name,)),))
    metadata = cargo_metadata(_WORKSPACE)

    with pytest.raises(InventoryDiscoveryError, match="cannot be attributed"):
        verify_test_target_attribution((_beta_integration(),), live, metadata)


def test_a_pinned_target_absent_from_cargo_metadata_is_refused() -> None:
    """An entry naming a target Cargo does not declare is refused."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
        PackageInventory,
        verify_test_target_attribution,
    )
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    ghost = InventoryHarness(
        package="beta",
        target="ghost",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )
    live = HarnessInventory((PackageInventory("beta", (ghost,)),))
    metadata = cargo_metadata(_WORKSPACE)
    selected = InventoryHarness(
        package="beta",
        target="ghost",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )

    with pytest.raises(InventoryDiscoveryError, match="does not"):
        verify_test_target_attribution((selected,), live, metadata)
