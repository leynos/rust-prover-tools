"""Resolve Cargo packages and targets, then discover their Kani proofs.

A repository is not an inventory. ``cargo metadata`` says which packages and
targets exist, and the source scan says which proofs each target declares. The
two are joined here so a consumer can inspect exactly what a run intends to
verify before any proof starts.

The selection is fail-closed at every step: an unknown package, a package with
no verifiable target, a package that resolves to no proofs, and a duplicate
identity across targets are all errors. Continuing past any of them would make
the aggregate report describe a set of harnesses the consumer never saw.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.kani_inventory.models import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    PackageInventory,
)
from rust_prover_tools.kani_inventory.rustsource import collect_proofs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "CargoPackage",
    "CargoTarget",
    "cargo_metadata",
    "discover_inventory",
    "select_packages",
    "test_targets",
    "verify_test_target_attribution",
)

#: Cargo target kinds Kani can verify, in the order this module prefers them.
_VERIFIABLE_KINDS: typ.Final[tuple[str, ...]] = ("lib", "bin", "test")


@dc.dataclass(frozen=True, slots=True)
class CargoTarget:
    """One Cargo target selected for discovery.

    Attributes
    ----------
    name : str
        Cargo target name.
    kind : str
        ``lib``, ``bin``, or ``test``.
    src_path : Path
        Crate root source file.
    """

    name: str
    kind: str
    src_path: Path


@dc.dataclass(frozen=True, slots=True)
class CargoPackage:
    """One Cargo package and its verifiable targets.

    Attributes
    ----------
    name : str
        Package name.
    manifest_path : Path
        Manifest that declared the package.
    targets : tuple[CargoTarget, ...]
        Verifiable targets in Cargo metadata order.
    """

    name: str
    manifest_path: Path
    targets: tuple[CargoTarget, ...]


def cargo_metadata(workspace_root: Path) -> tuple[CargoPackage, ...]:
    """Read the workspace's packages and verifiable targets from Cargo.

    Parameters
    ----------
    workspace_root : Path
        Directory in which to run ``cargo metadata``.

    Returns
    -------
    tuple[CargoPackage, ...]
        Every workspace package, including those this run does not select.

    Raises
    ------
    InventoryDiscoveryError
        Raised when Cargo cannot report the workspace or its output does not
        have the documented shape.
    """
    result = run_command(
        CommandSpec(
            ("cargo", "metadata", "--no-deps", "--format-version", "1"),
            cwd=workspace_root,
        )
    )
    if result.exit_code != 0:
        msg = (
            f"cargo metadata failed with status {result.exit_code}: "
            f"{result.stderr.strip()}"
        )
        raise InventoryDiscoveryError(msg)
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        msg = f"cargo metadata did not print JSON: {error}"
        raise InventoryDiscoveryError(msg) from error
    return _packages(document)


def _packages(document: object) -> tuple[CargoPackage, ...]:
    """Parse the packages array of a cargo metadata document."""
    root = _object(document, "cargo metadata")
    entries = root.get("packages")
    if not isinstance(entries, list):
        msg = "cargo metadata has no packages array"
        raise InventoryDiscoveryError(msg)
    return tuple(_package(entry) for entry in entries)


def _package(document: object) -> CargoPackage:
    """Parse one package entry."""
    root = _object(document, "a cargo package")
    name = _string(root.get("name"), "a package name")
    manifest = Path(_string(root.get("manifest_path"), f"{name} manifest path"))
    targets = root.get("targets")
    if not isinstance(targets, list):
        msg = f"package {name!r} has no targets array"
        raise InventoryDiscoveryError(msg)
    verifiable = tuple(
        target for entry in targets if (target := _target(entry)) is not None
    )
    return CargoPackage(name, manifest, verifiable)


def _target(document: object) -> CargoTarget | None:
    """Parse one target, or return `None` for a kind Kani cannot verify."""
    root = _object(document, "a cargo target")
    kinds = root.get("kind")
    if not isinstance(kinds, list):
        return None
    library_kinds = {"lib", "rlib", "dylib", "staticlib", "cdylib"}
    normalised = {"lib" if value in library_kinds else value for value in kinds}
    kind = next(
        (value for value in _VERIFIABLE_KINDS if value in normalised),
        None,
    )
    if kind is None:
        return None
    return CargoTarget(
        name=_string(root.get("name"), "a target name"),
        kind=kind,
        src_path=Path(_string(root.get("src_path"), "a target source path")),
    )


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        msg = f"{subject} is not an object"
        raise InventoryDiscoveryError(msg)
    return typ.cast("dict[str, object]", value)


def _string(value: object, subject: str) -> str:
    """Require a non-empty JSON string."""
    if not isinstance(value, str) or not value:
        msg = f"{subject} is not a non-empty string"
        raise InventoryDiscoveryError(msg)
    return value


def select_packages(
    packages: cabc.Sequence[CargoPackage],
    requested: cabc.Sequence[str],
) -> tuple[CargoPackage, ...]:
    """Select the requested packages, refusing unknown or duplicate names.

    Parameters
    ----------
    packages : Sequence[CargoPackage]
        Every package Cargo reported.
    requested : Sequence[str]
        Package names to discover. An empty sequence selects every package.

    Returns
    -------
    tuple[CargoPackage, ...]
        Selected packages in request order.

    Raises
    ------
    InventoryDiscoveryError
        Raised for an unknown or duplicated package name.
    """
    if not requested:
        return tuple(packages)
    by_name = {package.name: package for package in packages}
    unknown = [name for name in requested if name not in by_name]
    if unknown:
        msg = f"the workspace does not contain packages {unknown}"
        raise InventoryDiscoveryError(msg)
    if len(set(requested)) != len(requested):
        msg = f"the package selection names a package twice: {list(requested)}"
        raise InventoryDiscoveryError(msg)
    return tuple(by_name[name] for name in requested)


def discover_inventory(
    workspace_root: Path,
    packages: cabc.Sequence[str],
    *,
    metadata: cabc.Sequence[CargoPackage] | None = None,
) -> HarnessInventory:
    """Discover the expected inventory for the selected packages.

    Parameters
    ----------
    workspace_root : Path
        Repository or workspace root.
    packages : Sequence[str]
        Package names to discover.
    metadata : Sequence[CargoPackage] | None
        Pre-read Cargo metadata, for tests. `None` runs ``cargo metadata``.

    Returns
    -------
    HarnessInventory
        Package-qualified harnesses in deterministic order.

    Raises
    ------
    InventoryDiscoveryError
        Raised for an unknown package, a package with no verifiable target or
        no proofs, a duplicate identity, or an unresolvable source tree.
    """
    root = workspace_root.resolve()
    available = tuple(metadata) if metadata is not None else cargo_metadata(root)
    selected = select_packages(available, packages)
    inventories = tuple(_package_inventory(root, package) for package in selected)
    return HarnessInventory(
        tuple(sorted(inventories, key=lambda package: package.package))
    )


def _package_inventory(root: Path, package: CargoPackage) -> PackageInventory:
    """Discover one package's harnesses and refuse an empty or duplicate set."""
    if not package.targets:
        msg = (
            f"package {package.name!r} has no lib, bin, or test target Kani can verify"
        )
        raise InventoryDiscoveryError(msg)
    harnesses: list[InventoryHarness] = []
    for target in package.targets:
        harnesses.extend(_target_harnesses(root, package, target))
    if not harnesses:
        msg = f"package {package.name!r} resolves to no harnesses"
        raise InventoryDiscoveryError(msg)
    identities = [harness.harness for harness in harnesses]
    if len(set(identities)) != len(identities):
        msg = (
            f"package {package.name!r} declares a harness with the same "
            "pretty name in more than one target"
        )
        raise InventoryDiscoveryError(msg)
    ordered = tuple(
        sorted(harnesses, key=lambda item: (item.harness, item.target, item.line))
    )
    return PackageInventory(package.name, ordered)


def _target_harnesses(
    root: Path,
    package: CargoPackage,
    target: CargoTarget,
) -> list[InventoryHarness]:
    """Discover the proofs one target declares."""
    proofs = collect_proofs(target.src_path, root)
    return [
        InventoryHarness(
            package=package.name,
            target=target.name,
            target_kind=target.kind,
            harness=proof.pretty_name,
            source=proof.source,
            line=proof.line,
            conditional=proof.conditional,
            attributes=proof.attributes,
        )
        for proof in proofs
    ]


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
    packages: cabc.Sequence[CargoPackage],
) -> None:
    """Refuse a test-target harness that cannot be attributed exactly.

    Kani 0.67.0 has no flag that selects one integration-test target. Its
    ``--tests`` option enables verification of test crates and leaves every
    other target eligible, so a harness name alone does not say which test
    target produced a record. When the package declares exactly one test
    target, that target is the only possible source, and a same-named harness
    in the library or a binary would show up as a second selection record and
    be classified as a multiple match. With two or more test targets the same
    reasoning fails, so the run refuses rather than crediting a harness to a
    target the verifier did not necessarily build.

    Parameters
    ----------
    selected : Sequence[InventoryHarness]
        Harnesses the run intends to execute.
    packages : Sequence[CargoPackage]
        Live Cargo metadata, so the refusal reflects the tree being verified
        rather than only the inventory it was pinned from.

    Raises
    ------
    InventoryDiscoveryError
        Raised when a selected harness belongs to a test target and its
        package declares more than one test target.
    """
    counts = {name: len(names) for name, names in test_targets(packages).items()}
    for harness in selected:
        if harness.target_kind != "test":
            continue
        declared = counts.get(harness.package, 0)
        if declared > 1:
            msg = (
                f"package {harness.package!r} declares {declared} integration-test "
                f"targets, so harness {harness.harness!r} in target "
                f"{harness.target!r} cannot be attributed exactly: Kani 0.67.0 "
                "has no flag that selects a single test target. Select a lib or "
                "bin harness, or split the test targets."
            )
            raise InventoryDiscoveryError(msg)
