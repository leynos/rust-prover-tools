"""Typed, inspectable Kani harness inventories and their strict JSON form.

An inventory is the list of proofs a run intends to execute. It is
package-qualified because two packages may legitimately declare a harness with
the same name, and it is inspectable because a consumer must be able to see the
exact selection before a proof starts.

The version-1 document is deliberately strict: every key is required, no
unknown key is permitted, an empty package is refused, and a duplicate
``package::harness`` identity is refused. A lenient reader would let a
hand-edited inventory silently select fewer proofs than its author intended.

Examples
--------
>>> harness = InventoryHarness(
...     package="p", target="p", target_kind="lib",
...     harness="harnesses::x", source="src/x.rs", line=3,
... )
>>> harness.identity
'p::harnesses::x'
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.models import HarnessSpec

__all__ = (
    "SCHEMA_VERSION",
    "HarnessInventory",
    "InventoryDiscoveryError",
    "InventoryFormatError",
    "InventoryHarness",
    "PackageInventory",
)

#: Version of the inventory document schema.
SCHEMA_VERSION: typ.Final[int] = 1


class InventoryDiscoveryError(ValueError):
    """A source tree or package selection that cannot yield a valid inventory."""


class InventoryFormatError(ValueError):
    """An inventory document that does not match the strict version-1 schema."""


@dc.dataclass(frozen=True, slots=True)
class InventoryHarness:
    """One proof harness resolved to an exact Kani selector.

    Attributes
    ----------
    package : str
        Cargo package that owns the harness.
    target : str
        Cargo target name the proof belongs to.
    target_kind : str
        ``lib``, ``bin``, or ``test``.
    harness : str
        Fully qualified Kani pretty name used with ``--harness``.
    source : str
        Repository-relative source file declaring the proof.
    line : int
        One-based line of the proof function.
    conditional : bool
        Whether an enclosing ``cfg`` other than ``cfg(kani)`` gates the proof.
        Discovery does not evaluate ``cfg``; a conditional proof is listed and
        may fail selection if the selected build configuration omits it.
    attributes : tuple[str, ...]
        Proof attributes that selected the item, such as ``kani::proof``.
    """

    package: str
    target: str
    target_kind: str
    harness: str
    source: str
    line: int
    conditional: bool = False
    attributes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an identity that cannot be selected or reported."""
        if not (self.package and self.harness and self.target):
            msg = "an inventory harness needs a package, a target, and a name"
            raise ValueError(msg)
        if self.target_kind not in {"lib", "bin", "test"}:
            msg = f"unknown target kind {self.target_kind!r}"
            raise ValueError(msg)
        if self.line < 1:
            msg = "an inventory harness needs a positive source line"
            raise ValueError(msg)

    @property
    def identity(self) -> str:
        """The package-qualified identity used in accounting."""
        return f"{self.package}::{self.harness}"

    def to_spec(self, attempt: int = 1) -> HarnessSpec:
        """Return the execution identity for this harness.

        Parameters
        ----------
        attempt : int
            One-based attempt ordinal.

        Returns
        -------
        HarnessSpec
            The identity the supervision layer runs.
        """
        return HarnessSpec(
            package=self.package,
            harness=self.harness,
            attempt=attempt,
        )


@dc.dataclass(frozen=True, slots=True)
class PackageInventory:
    """The harnesses one selected package contributes.

    Attributes
    ----------
    package : str
        Cargo package name.
    harnesses : tuple[InventoryHarness, ...]
        Harnesses in deterministic order.
    """

    package: str
    harnesses: tuple[InventoryHarness, ...]

    def __post_init__(self) -> None:
        """Refuse an empty package or one with a duplicate identity."""
        if not self.harnesses:
            msg = f"package {self.package!r} resolves to no harnesses"
            raise ValueError(msg)
        names = [harness.harness for harness in self.harnesses]
        if len(set(names)) != len(names):
            msg = f"package {self.package!r} declares a harness twice"
            raise ValueError(msg)
        for harness in self.harnesses:
            if harness.package != self.package:
                msg = (
                    f"harness {harness.harness!r} belongs to "
                    f"{harness.package!r}, not {self.package!r}"
                )
                raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class HarnessInventory:
    """The complete expected inventory for a run.

    Attributes
    ----------
    packages : tuple[PackageInventory, ...]
        Selected packages in deterministic order.
    """

    packages: tuple[PackageInventory, ...]

    def __post_init__(self) -> None:
        """Refuse an empty inventory, a duplicate package, or duplicate identities."""
        if not self.packages:
            msg = "an inventory declares no packages"
            raise ValueError(msg)
        names = [package.package for package in self.packages]
        if len(set(names)) != len(names):
            msg = "an inventory declares a package twice"
            raise ValueError(msg)
        identities = [harness.identity for harness in self.harnesses]
        if len(set(identities)) != len(identities):
            msg = "an inventory declares a package-qualified harness twice"
            raise ValueError(msg)

    @property
    def harnesses(self) -> tuple[InventoryHarness, ...]:
        """Every harness in package and name order."""
        return tuple(
            harness for package in self.packages for harness in package.harnesses
        )

    def identities(self) -> tuple[str, ...]:
        """Return every package-qualified identity, in inventory order."""
        return tuple(harness.identity for harness in self.harnesses)

    def specs(self) -> tuple[HarnessSpec, ...]:
        """Return the execution identities for every harness."""
        return tuple(harness.to_spec() for harness in self.harnesses)
