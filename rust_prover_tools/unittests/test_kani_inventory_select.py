"""Hold exact harness selection and argv composition to its conflict matrix."""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    PackageInventory,
    SelectionError,
    compose_arguments,
    conflicting_options,
    restrict_inventory,
    select_harnesses,
    selections,
)
from rust_prover_tools.kani_log import KANI_067


def _harness(
    harness: str,
    package: str = "alpha",
    target: str = "alpha",
    target_kind: str = "lib",
) -> InventoryHarness:
    """Return one inventory harness."""
    return InventoryHarness(
        package=package,
        target=target,
        target_kind=target_kind,
        harness=harness,
        source="src/lib.rs",
        line=1,
        attributes=("kani::proof",),
    )


def _inventory() -> HarnessInventory:
    """Return an inventory spanning two packages and a shared leaf name."""
    return HarnessInventory((
        PackageInventory(
            "alpha",
            (
                _harness("harnesses::a"),
                _harness("harnesses::b"),
                _harness("shared::proof"),
            ),
        ),
        PackageInventory(
            "beta",
            (
                _harness("in_main", package="beta", target="beta"),
                _harness("shared::proof", package="beta", target="beta"),
            ),
        ),
    ))


def test_an_empty_request_selects_every_harness() -> None:
    """The default selection is the whole inventory, in inventory order."""
    inventory = _inventory()

    assert select_harnesses(inventory, ()) == inventory.harnesses


def test_an_exact_pretty_name_selects_one_harness() -> None:
    """A fully qualified name identifies exactly one harness."""
    chosen = select_harnesses(_inventory(), ("harnesses::b",))

    assert [harness.identity for harness in chosen] == ["alpha::harnesses::b"]


def test_a_package_qualified_name_resolves_a_shared_leaf() -> None:
    """The qualified identity disambiguates two packages' `shared::proof`."""
    chosen = select_harnesses(_inventory(), ("beta::shared::proof",))

    assert [harness.package for harness in chosen] == ["beta"]


def test_a_shared_leaf_name_is_ambiguous() -> None:
    """A bare name matching two packages is refused, not guessed."""
    with pytest.raises(SelectionError, match="ambiguous"):
        select_harnesses(_inventory(), ("shared::proof",))


def test_a_missing_name_is_refused() -> None:
    """A name the inventory does not declare cannot be selected."""
    with pytest.raises(SelectionError, match="does not declare"):
        select_harnesses(_inventory(), ("harnesses::missing",))


def test_a_duplicate_request_is_refused() -> None:
    """Selecting one harness twice would run it twice."""
    with pytest.raises(SelectionError, match="twice"):
        select_harnesses(_inventory(), ("harnesses::a", "harnesses::a"))


def test_two_spellings_of_one_harness_are_refused() -> None:
    """A pretty name and its qualified form select the same harness."""
    with pytest.raises(SelectionError, match="more than one spelling"):
        select_harnesses(_inventory(), ("harnesses::a", "alpha::harnesses::a"))


def test_the_composed_arguments_put_the_selector_first() -> None:
    """The selector, the output arguments, and the caller's arguments in order."""
    arguments = compose_arguments(_harness("harnesses::a"), KANI_067)

    assert arguments == (
        "--package",
        "alpha",
        "--harness",
        "harnesses::a",
        "--exact",
        "--lib",
        "--output-format=regular",
        "--jobs=1",
    )


def test_a_test_target_composes_the_tests_flag() -> None:
    """Kani only builds an integration-test target when `--tests` is set."""
    arguments = compose_arguments(
        _harness("integration::proof", target="integration", target_kind="test"),
        KANI_067,
    )

    assert "--tests" in arguments
    assert arguments.index("--tests") < arguments.index("--output-format=regular")


def test_proof_affecting_arguments_are_preserved() -> None:
    """Unwinding flags and unstable options pass through unchanged."""
    arguments = compose_arguments(
        _harness("harnesses::a"),
        KANI_067,
        ("--unwind", "3", "-Z", "unstable-options", "--default-unwind", "2"),
    )

    assert arguments[-6:] == (
        "--unwind",
        "3",
        "-Z",
        "unstable-options",
        "--default-unwind",
        "2",
    )


@pytest.mark.parametrize("prefix", [(), ("--",)])
@pytest.mark.parametrize(
    "conflict",
    [
        ("--harness", "other"),
        ("--harness=other",),
        ("--exact",),
        ("--package", "beta"),
        ("--package=beta",),
        ("-p", "beta"),
        ("-pbeta",),
        ("--workspace",),
        ("-j", "4"),
        ("--jobs", "4"),
        ("--output-format", "json"),
        ("--output-format=json",),
        ("--output-into-files",),
        ("--quiet",),
        ("-q",),
    ],
)
def test_every_conflicting_spelling_is_refused(
    prefix: tuple[str, ...], conflict: tuple[str, ...]
) -> None:
    """A caller argument cannot override the selection or the parsed output."""
    arguments = (*prefix, *conflict)
    found = conflicting_options(arguments, KANI_067)
    assert found
    assert found[0] == conflict[0]

    with pytest.raises(SelectionError, match="override"):
        compose_arguments(_harness("harnesses::a"), KANI_067, arguments)


def test_a_conflict_before_the_separator_is_refused() -> None:
    """The driver side of ``--`` is where selection lives."""
    arguments = ("--output-format=terse", "--unwind", "2")

    assert conflicting_options(arguments, KANI_067) == ("--output-format=terse",)


def test_a_non_conflicting_argument_list_is_accepted() -> None:
    """Ordinary verifier options do not conflict."""
    arguments = ("--verbose", "--solver", "z3", "-Z", "unstable-options")

    assert not conflicting_options(arguments, KANI_067)
    assert compose_arguments(_harness("harnesses::a"), KANI_067, arguments)[-5:] == (
        arguments
    )


def test_one_selection_is_composed_per_harness() -> None:
    """Every selected harness gets its own exact argument vector."""
    chosen = select_harnesses(_inventory(), ("harnesses::a", "harnesses::b"))

    composed = selections(chosen, KANI_067)

    assert [selection.identity for selection in composed] == [
        "alpha::harnesses::a",
        "alpha::harnesses::b",
    ]
    assert all("--exact" in selection.arguments for selection in composed)
    assert composed[0].arguments[3] == "harnesses::a"
    assert composed[1].arguments[3] == "harnesses::b"


def test_an_empty_selection_set_is_refused() -> None:
    """No harnesses means no run, not an empty success."""
    with pytest.raises(SelectionError, match="no harness was selected"):
        selections((), KANI_067)


def test_a_duplicate_package_restriction_is_refused() -> None:
    """Naming one package twice is a request error, not a duplicate model."""
    with pytest.raises(InventoryDiscoveryError, match="twice"):
        restrict_inventory(_inventory(), ("alpha", "alpha"))


def test_a_name_matching_two_proofs_is_ambiguous() -> None:
    """An identity and another harness's pretty name must not collide."""
    inventory = HarnessInventory((
        PackageInventory("p", (_harness("x", package="p", target="p"),)),
        PackageInventory("q", (_harness("p::x", package="q", target="q"),)),
    ))

    with pytest.raises(SelectionError, match="ambiguous"):
        select_harnesses(inventory, ("p::x",))

    (chosen,) = select_harnesses(restrict_inventory(inventory, ("p",)), ("p::x",))
    assert chosen.package == "p"


def test_a_binary_target_composes_its_name() -> None:
    """A binary selector names the target Kani must build."""
    arguments = compose_arguments(
        _harness("bin::proof", target="tool", target_kind="bin"), KANI_067
    )

    assert (
        arguments[:9]
        == (
            "--package",
            "alpha",
            "--harness",
            "bin::proof",
            "--exact",
            "--bin",
            "tool",
            "--output-format=regular",
            "--jobs=1",
        )[:9]
    )
