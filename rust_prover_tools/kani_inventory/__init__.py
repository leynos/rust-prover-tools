"""Inspectable, package-qualified Kani harness inventories.

The package separates five concerns:

- ``models`` holds the typed inventory values and the refusals that keep an
  ambiguous or empty inventory from being constructed.
- ``document`` reads and writes the strict version-1 JSON interface.
- ``masking`` blanks comments and literals so source scanning cannot mistake
  their contents for code.
- ``items`` parses one module level into proofs, file modules, and inline
  modules.
- ``rustsource`` follows Rust 2018 module declarations, including ``#[path]``,
  and collects every reachable proof.
- ``discover`` resolves Cargo packages and targets and produces an inventory.
- ``select`` chooses exact harnesses and composes the per-harness argv.

Discovery never evaluates ``cfg``; a proof gated by anything other than
``cfg(kani)`` is listed as conditional, and the run fails closed if Kani does
not select the identity the inventory names.
"""

from __future__ import annotations

from rust_prover_tools.kani_inventory.discover import (
    CargoPackage,
    CargoTarget,
    cargo_metadata,
    discover_inventory,
    select_packages,
    test_targets,
    verify_test_target_attribution,
)
from rust_prover_tools.kani_inventory.document import (
    inventory_document,
    inventory_from_document,
    load_inventory,
    write_inventory,
)
from rust_prover_tools.kani_inventory.models import (
    SCHEMA_VERSION,
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryFormatError,
    InventoryHarness,
    PackageInventory,
)
from rust_prover_tools.kani_inventory.rustsource import SourceProof, collect_proofs
from rust_prover_tools.kani_inventory.select import (
    Selection,
    SelectionError,
    compose_arguments,
    conflicting_options,
    restrict_inventory,
    select_harnesses,
    selections,
)

__all__ = [
    "SCHEMA_VERSION",
    "CargoPackage",
    "CargoTarget",
    "HarnessInventory",
    "InventoryDiscoveryError",
    "InventoryFormatError",
    "InventoryHarness",
    "PackageInventory",
    "Selection",
    "SelectionError",
    "SourceProof",
    "cargo_metadata",
    "collect_proofs",
    "compose_arguments",
    "conflicting_options",
    "discover_inventory",
    "inventory_document",
    "inventory_from_document",
    "load_inventory",
    "restrict_inventory",
    "select_harnesses",
    "select_packages",
    "selections",
    "test_targets",
    "verify_test_target_attribution",
    "write_inventory",
]
