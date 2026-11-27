"""Hold inventory discovery and its Cargo metadata to fail-closed behaviour."""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.kani_inventory import (
    InventoryDiscoveryError,
    discover_inventory,
)
from rust_prover_tools.unittests.kani_inventory_discover_support import (
    WORKSPACE as _WORKSPACE,
)
from rust_prover_tools.unittests.kani_inventory_discover_support import (
    inventory as _inventory,
)

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox

pytest_plugins = ("cmd_mox.pytest_plugin",)


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


def test_cargo_metadata_reads_a_targets_required_features(
    cmd_mox: CmdMox,
) -> None:
    """A target's required-features array is carried into CargoTarget."""
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    document = {
        "packages": [
            {
                "name": "beta",
                "manifest_path": str(_WORKSPACE / "beta" / "Cargo.toml"),
                "targets": [
                    {
                        "name": "integration",
                        "kind": ["test"],
                        "src_path": str(
                            _WORKSPACE / "beta" / "tests" / "integration.rs"
                        ),
                        "required-features": ["gated"],
                    }
                ],
            }
        ]
    }
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stdout=json.dumps(document))

    metadata = cargo_metadata(_WORKSPACE)

    (target,) = metadata[0].targets
    assert target.required_features == ("gated",)


def test_a_non_array_required_features_field_is_refused(
    cmd_mox: CmdMox,
) -> None:
    """A malformed metadata field fails closed instead of reading as empty."""
    document = {
        "packages": [
            {
                "name": "beta",
                "manifest_path": str(_WORKSPACE / "beta" / "Cargo.toml"),
                "targets": [
                    {
                        "name": "integration",
                        "kind": ["test"],
                        "src_path": str(
                            _WORKSPACE / "beta" / "tests" / "integration.rs"
                        ),
                        "required-features": "gated",
                    }
                ],
            }
        ]
    }
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stdout=json.dumps(document))

    with pytest.raises(InventoryDiscoveryError, match="non-array"):
        discover_inventory(_WORKSPACE, ("beta",))


def test_a_failed_metadata_call_is_refused(cmd_mox: CmdMox) -> None:
    """A Cargo failure names the status and the diagnostic."""
    cmd_mox.mock("cargo").with_args(
        "metadata", "--no-deps", "--format-version", "1"
    ).returns(stderr="manifest missing\n", exit_code=101)

    with pytest.raises(InventoryDiscoveryError, match="status 101"):
        discover_inventory(_WORKSPACE, ("alpha",))


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


def test_the_fixture_inventory_is_exactly_what_the_suite_expects() -> None:
    """Pin every package-qualified identity and its target mapping.

    The proof assessment requires a complete inventory assertion rather than
    per-target subsets. This records the whole set, including the binary helper
    proof and the integration-test proof.
    """
    inventory = _inventory("alpha", "beta")

    observed = {
        (harness.identity, harness.target, harness.target_kind)
        for harness in inventory.harnesses
    }

    assert observed == {
        ("alpha::harnesses::bounded", "alpha", "lib"),
        ("alpha::harnesses::conditional_extra", "alpha", "lib"),
        ("alpha::harnesses::failing", "alpha", "lib"),
        ("alpha::harnesses::nested::proof", "alpha", "lib"),
        ("alpha::harnesses::verified", "alpha", "lib"),
        ("alpha::inline::inside", "alpha", "lib"),
        ("alpha::path_proofs::via_path", "alpha", "lib"),
        ("alpha::shared::proof", "alpha", "lib"),
        ("beta::helper::in_helper", "beta", "bin"),
        ("beta::in_main", "beta", "bin"),
        ("beta::gated_proof", "integration", "test"),
        ("beta::integration_proof", "integration", "test"),
    }
