"""Choose exact harnesses and compose their verifier argument vectors.

``--harness NAME`` without ``--exact`` is a substring filter, and a bare name
can match several proofs in one package. The supervisor therefore always
passes ``--package``, ``--harness``, and ``--exact``, and refuses any caller
argument that would override the selection or move the report out of the
parsed stream.

Composition order matters: the registry's output arguments come before the
caller's, the selector comes before both, and everything after ``--`` is the
verifier's own. A conflict is detected in the separate-value form
(``--harness value``), the joined form (``--harness=value``), and the
short-option joined form (``-pvalue``), on either side of ``--``.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_inventory.models import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log import ForbiddenOption, KaniGrammar

__all__ = (
    "Selection",
    "SelectionError",
    "compose_arguments",
    "conflicting_options",
    "restrict_inventory",
    "select_harnesses",
)


class SelectionError(ValueError):
    """A harness selection that is ambiguous, missing, or overridden."""


@dc.dataclass(frozen=True, slots=True)
class Selection:
    """One exact harness selection.

    Attributes
    ----------
    harness : InventoryHarness
        Selected inventory entry.
    arguments : tuple[str, ...]
        Verifier arguments for this harness.
    """

    harness: InventoryHarness
    arguments: tuple[str, ...]

    @property
    def identity(self) -> str:
        """The package-qualified identity this selection runs."""
        return self.harness.identity


def select_harnesses(
    inventory: HarnessInventory,
    requested: cabc.Sequence[str],
) -> tuple[InventoryHarness, ...]:
    """Select the requested harnesses by exact pretty name.

    Parameters
    ----------
    inventory : HarnessInventory
        Inventory to select from.
    requested : Sequence[str]
        Pretty names to select. An empty sequence selects every harness.

    Returns
    -------
    tuple[InventoryHarness, ...]
        Selected harnesses in inventory order.

    Raises
    ------
    SelectionError
        Raised when a requested name is declared by more than one package or
        appears twice, or when a requested name is not in the inventory.
    """
    if not requested:
        return inventory.harnesses
    if len(set(requested)) != len(requested):
        msg = f"the harness selection names a harness twice: {list(requested)}"
        raise SelectionError(msg)
    by_name = _index(inventory)
    chosen: list[InventoryHarness] = []
    for name in requested:
        matches = by_name.get(name, [])
        if not matches:
            msg = f"the inventory does not declare harness {name!r}"
            raise SelectionError(msg)
        if len(matches) > 1:
            packages = sorted(harness.identity for harness in matches)
            msg = (
                f"harness {name!r} is ambiguous: it matches {packages}; "
                "select it by its package-qualified pretty name"
            )
            raise SelectionError(msg)
        chosen.append(matches[0])
    return tuple(chosen)


def _index(inventory: HarnessInventory) -> dict[str, list[InventoryHarness]]:
    """Index harnesses by pretty name, identity, and unique leaf name."""
    index: dict[str, list[InventoryHarness]] = {}
    for harness in inventory.harnesses:
        keys = {harness.harness, harness.identity}
        leaf = harness.harness.rsplit("::", 1)[-1]
        keys.add(leaf)
        for key in keys:
            index.setdefault(key, []).append(harness)
    return index


def conflicting_options(
    arguments: cabc.Sequence[str],
    grammar: KaniGrammar,
) -> tuple[str, ...]:
    """Return caller arguments that would override selection or output.

    Parameters
    ----------
    arguments : Sequence[str]
        Caller arguments, including anything after ``--``.
    grammar : KaniGrammar
        Registered grammar whose forbidden spellings apply.

    Returns
    -------
    tuple[str, ...]
        The offending arguments, in the order they appear. An empty tuple
        means nothing conflicts.
    """
    found: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        option = _match_option(argument, grammar)
        if option is None:
            index += 1
            continue
        found.append(argument)
        index += 1 if _is_joined(argument, option) or not option.takes_value else 2
    return tuple(found)


def _match_option(
    argument: str,
    grammar: KaniGrammar,
) -> ForbiddenOption | None:
    """Return the forbidden option an argument spells, if any."""
    for option in grammar.forbidden_options:
        if argument == option.long:
            return option
        if argument.startswith(option.long + "="):
            return option
        if option.short is not None:
            if argument == option.short:
                return option
            if argument.startswith(option.short) and len(argument) > len(option.short):
                return option
    return None


def _is_joined(argument: str, option: ForbiddenOption) -> bool:
    """Whether the argument supplies the option's value in the same token."""
    if argument.startswith(option.long + "="):
        return True
    return (
        option.short is not None
        and argument.startswith(option.short)
        and (len(argument) > len(option.short))
    )


def compose_arguments(
    harness: InventoryHarness,
    grammar: KaniGrammar,
    caller_arguments: cabc.Sequence[str] = (),
) -> tuple[str, ...]:
    """Compose the verifier arguments for one exact harness.

    Parameters
    ----------
    harness : InventoryHarness
        Harness to select.
    grammar : KaniGrammar
        Registered grammar supplying the output arguments and forbidden
        spellings.
    caller_arguments : Sequence[str]
        Additional verifier arguments. Proof-affecting flags such as
        ``--unwind`` and ``-Z`` are preserved.

    Returns
    -------
    tuple[str, ...]
        The composed argument vector, selector first.

    Raises
    ------
    SelectionError
        Raised when a caller argument would override the selection or the
        parsed output.
    """
    conflicts = conflicting_options(caller_arguments, grammar)
    if conflicts:
        msg = (
            "the caller supplied verifier arguments that would override the "
            f"supervised selection or output: {list(conflicts)}"
        )
        raise SelectionError(msg)
    target_arguments = ("--tests",) if harness.target_kind == "test" else ()
    return (
        "--package",
        harness.package,
        "--harness",
        harness.harness,
        "--exact",
        *target_arguments,
        *grammar.output_arguments,
        *caller_arguments,
    )


def selections(
    harnesses: cabc.Sequence[InventoryHarness],
    grammar: KaniGrammar,
    caller_arguments: cabc.Sequence[str] = (),
) -> tuple[Selection, ...]:
    """Compose one selection per harness.

    Parameters
    ----------
    harnesses : Sequence[InventoryHarness]
        Harnesses to select.
    grammar : KaniGrammar
        Registered grammar.
    caller_arguments : Sequence[str]
        Additional verifier arguments.

    Returns
    -------
    tuple[Selection, ...]
        One exact selection per harness.

    Raises
    ------
    InventoryDiscoveryError
        Raised when no harness was selected.
    SelectionError
        Raised when a caller argument conflicts.
    """
    if not harnesses:
        msg = "no harness was selected"
        raise SelectionError(msg)
    return tuple(
        Selection(
            harness=harness,
            arguments=compose_arguments(harness, grammar, caller_arguments),
        )
        for harness in harnesses
    )


def restrict_inventory(
    inventory: HarnessInventory,
    packages: cabc.Sequence[str],
) -> HarnessInventory:
    """Narrow an inventory to the requested packages.

    Parameters
    ----------
    inventory : HarnessInventory
        Authoritative inventory.
    packages : cabc.Sequence[str]
        Package names to keep. An empty sequence keeps everything.

    Returns
    -------
    HarnessInventory
        The narrowed inventory.

    Raises
    ------
    InventoryDiscoveryError
        Raised when a requested package is not in the inventory.
    """
    if not packages:
        return inventory
    by_name = {package.package: package for package in inventory.packages}
    unknown = [name for name in packages if name not in by_name]
    if unknown:
        msg = f"the inventory does not contain packages {unknown}"
        raise InventoryDiscoveryError(msg)
    return HarnessInventory(tuple(by_name[name] for name in packages))
