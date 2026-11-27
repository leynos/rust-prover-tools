"""Hold test-target attribution to the live workspace's exact metadata."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    PackageInventory,
    discover_inventory,
    verify_test_target_attribution,
)
from rust_prover_tools.unittests.kani_inventory_discover_support import (
    WORKSPACE as _WORKSPACE,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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
    live, metadata = _live_beta()

    verify_test_target_attribution((_beta_integration(),), live, metadata)


def test_only_packages_with_selected_test_harnesses_are_rediscovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lib-only selection does not force its package through a fresh scan."""
    from rust_prover_tools.kani_inventory import discover as discover_module
    from rust_prover_tools.kani_inventory import verify_selected_test_targets

    live, metadata = _live_beta()
    beta_harness = _beta_integration()
    alpha = discover_inventory(_WORKSPACE, ("alpha",), metadata=metadata)
    lib_harness = next(
        harness for harness in alpha.harnesses if harness.target_kind == "lib"
    )
    requested: list[tuple[str, ...]] = []

    def recording_discover(
        root: Path,
        packages: cabc.Iterable[str],
        *,
        metadata: cabc.Iterable[typ.Any] | None = None,
    ) -> HarnessInventory:
        """Record the packages the attribution check thinks it needs."""
        del root, metadata
        requested.append(tuple(packages))
        return live

    monkeypatch.setattr(discover_module, "discover_inventory", recording_discover)

    verify_selected_test_targets(
        _WORKSPACE,
        (lib_harness, beta_harness),
        metadata,
    )

    assert requested == [("beta",)]
    assert beta_harness.identity in live.identities()


def test_a_pinned_test_harness_absent_from_the_live_tree_is_refused() -> None:
    """A removed harness must not be credited to a stale inventory entry."""
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


def test_multiple_test_targets_are_refused_even_when_the_entry_matches() -> None:
    """D14 requires the multi-test-target refusal, not only a live match."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
    )
    from rust_prover_tools.kani_inventory.discover import (
        CargoPackage,
        CargoTarget,
        cargo_metadata,
    )

    harness = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )
    live = HarnessInventory((PackageInventory("beta", (harness,)),))
    metadata = list(cargo_metadata(_WORKSPACE))
    beta = next(package for package in metadata if package.name == "beta")
    doubled = tuple(
        CargoPackage(
            name=package.name,
            manifest_path=package.manifest_path,
            targets=package.targets
            + (
                (
                    CargoTarget(
                        name="second",
                        kind="test",
                        src_path=package.targets[0].src_path,
                    ),
                )
                if package.name == "beta"
                else ()
            ),
        )
        for package in metadata
    )
    del beta

    with pytest.raises(InventoryDiscoveryError, match="no flag"):
        verify_test_target_attribution((harness,), live, doubled)


def test_a_conditional_test_harness_is_refused() -> None:
    """A cfg-gated test harness cannot be attributed to its target."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
    )
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    harness = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
        conditional=True,
    )
    live = HarnessInventory((PackageInventory("beta", (harness,)),))
    metadata = cargo_metadata(_WORKSPACE)

    with pytest.raises(InventoryDiscoveryError, match="cfg gate"):
        verify_test_target_attribution((harness,), live, metadata)


def test_a_freshly_conditional_test_harness_is_refused() -> None:
    """A stale unconditional entry is refused when the live harness is gated."""
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
    )
    from rust_prover_tools.kani_inventory.discover import cargo_metadata

    pinned = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )
    fresh = dc.replace(pinned, conditional=True)
    live = HarnessInventory((PackageInventory("beta", (fresh,)),))
    metadata = cargo_metadata(_WORKSPACE)

    with pytest.raises(InventoryDiscoveryError, match="cfg gate"):
        verify_test_target_attribution((pinned,), live, metadata)


def test_a_discovered_conditional_test_harness_is_refused_when_pinned() -> None:
    """Discovery marks the gate, and a stale unconditional pin is refused."""
    live, metadata = _live_beta()
    gated = next(
        harness for harness in live.harnesses if harness.harness == "gated_proof"
    )
    assert gated.conditional
    pinned = dc.replace(gated, conditional=False)

    with pytest.raises(InventoryDiscoveryError, match="cfg gate"):
        verify_test_target_attribution((pinned,), live, metadata)


def test_a_test_target_with_required_features_is_refused() -> None:
    """Features this run does not enable make the target unattributable."""
    from rust_prover_tools.kani_inventory.discover import CargoPackage

    live, metadata = _live_beta()
    gated = tuple(
        CargoPackage(
            name=package.name,
            manifest_path=package.manifest_path,
            targets=tuple(
                dc.replace(target, required_features=("gated",))
                if package.name == "beta" and target.kind == "test"
                else target
                for target in package.targets
            ),
        )
        for package in metadata
    )

    with pytest.raises(InventoryDiscoveryError, match="requires features"):
        verify_test_target_attribution((_beta_integration(),), live, gated)


@pytest.mark.parametrize("same_name_first", [True, False])
def test_a_same_named_library_does_not_answer_for_the_test_target(
    *, same_name_first: bool
) -> None:
    """The feature guard reads the test target, whatever metadata order holds.

    Cargo permits a library and a test target with one name. Indexing targets
    by name alone would let the library answer for the test target and hide
    its ``required-features``, so both metadata orders are exercised.
    """
    from rust_prover_tools.kani_inventory.discover import (
        CargoPackage,
        CargoTarget,
    )

    live, metadata = _live_beta()
    test = CargoTarget(
        name="integration",
        kind="test",
        src_path=_WORKSPACE / "beta" / "tests" / "integration.rs",
        required_features=("gated",),
    )
    library = CargoTarget(
        name="integration",
        kind="lib",
        src_path=_WORKSPACE / "beta" / "src" / "lib.rs",
    )
    ordered = (test, library) if same_name_first else (library, test)
    packages = tuple(
        CargoPackage(
            name=package.name,
            manifest_path=package.manifest_path,
            targets=tuple(target for target in package.targets if target.kind != "test")
            + ordered
            if package.name == "beta"
            else package.targets,
        )
        for package in metadata
    )

    with pytest.raises(InventoryDiscoveryError, match="requires features"):
        verify_test_target_attribution((_beta_integration(),), live, packages)
