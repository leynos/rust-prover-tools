"""Attribute a pinned test-target harness to the tree the run will build.

Kani 0.67.0 has no flag that selects one integration-test target, so a stored
inventory entry cannot be trusted on its own. These checks compare it with a
fresh discovery of the same tree and with the live Cargo metadata, and refuse
anything the verifier could credit to a different target.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_inventory.models import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_inventory.discover import CargoPackage

__all__ = (
    "test_targets",
    "verify_test_target_attribution",
)


def test_targets(packages: cabc.Sequence[CargoPackage]) -> dict[str, tuple[str, ...]]:
    """Return each package's integration-test target names.

    Parameters
    ----------
    packages : Sequence[CargoPackage]
        Packages read from Cargo metadata.

    Returns
    -------
    dict[str, tuple[str, ...]]
        Test target names keyed by package name.
    """
    return {
        package.name: tuple(
            target.name for target in package.targets if target.kind == "test"
        )
        for package in packages
    }


def verify_test_target_attribution(
    selected: cabc.Sequence[InventoryHarness],
    live: HarnessInventory,
    packages: cabc.Sequence[CargoPackage],
) -> None:
    """Refuse a test-target harness the live tree does not attribute exactly.

    Kani 0.67.0 has no flag that selects one integration-test target. Its
    ``--tests`` option enables verification of test crates and leaves every
    other target eligible, so a single matching verifier record does not say
    which Cargo target produced it. The only safe rule is to compare the pinned
    entry with a fresh discovery of the same tree: the identity must exist as a
    test-target harness with the same target name. A removed, renamed, or
    moved harness is refused rather than credited to the stale entry.

    Parameters
    ----------
    selected : Sequence[InventoryHarness]
        Harnesses the run intends to execute.
    live : HarnessInventory
        Inventory discovered from the tree the run will build.
    packages : Sequence[CargoPackage]
        Cargo metadata the live inventory was read from.

    Raises
    ------
    InventoryDiscoveryError
        Raised when a selected test-target harness is absent from the live
        tree, has moved to another target or target kind, or sits in a package
        whose test targets are not uniquely attributable.
    """
    by_identity = {harness.identity: harness for harness in live.harnesses}
    declared = test_targets(packages)
    for harness in selected:
        if harness.target_kind != "test":
            continue
        count = len(declared.get(harness.package, ()))
        if count > 1:
            msg = (
                f"package {harness.package!r} declares {count} integration-test "
                f"targets, so harness {harness.harness!r} cannot be attributed "
                "exactly: Kani 0.67.0 has no flag that selects one test target."
            )
            raise InventoryDiscoveryError(msg)
        if harness.conditional:
            msg = (
                f"the inventory records {harness.identity!r} behind a cfg gate "
                "in test target "
                f"{harness.target!r}. A cfg-disabled harness can be absent "
                "while a same-named generated harness elsewhere is selected, so "
                "the entry cannot be attributed."
            )
            raise InventoryDiscoveryError(msg)
        actual = by_identity.get(harness.identity)
        if actual is None:
            msg = (
                f"the inventory records {harness.identity!r} in test target "
                f"{harness.target!r}, but the live tree declares no such "
                "harness. Refusing to credit a stale inventory."
            )
            raise InventoryDiscoveryError(msg)
        if actual.target_kind != "test" or actual.target != harness.target:
            msg = (
                f"the inventory records {harness.identity!r} in "
                f"{harness.target_kind} target {harness.target!r}, but the live "
                f"tree declares it in {actual.target_kind} target "
                f"{actual.target!r}. Kani 0.67.0 cannot select one test target, "
                "so the stale entry cannot be attributed."
            )
            raise InventoryDiscoveryError(msg)
        if harness.target not in declared.get(harness.package, ()):
            msg = (
                f"the inventory records {harness.identity!r} in test target "
                f"{harness.target!r}, which the live Cargo metadata does not "
                f"declare for package {harness.package!r}."
            )
            raise InventoryDiscoveryError(msg)
