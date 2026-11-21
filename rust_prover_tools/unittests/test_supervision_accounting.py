"""Hold the multiset comparison to every kind of identity drift."""

from __future__ import annotations

import pytest

from rust_prover_tools.supervision import compare_identities, identity_key


def test_an_identity_key_is_package_qualified() -> None:
    """Two packages may legitimately use the same harness name."""
    assert identity_key("a", "harnesses::x") == "a::harnesses::x"
    assert identity_key("a", "h") != identity_key("b", "h")


def test_equal_multisets_have_no_drift() -> None:
    """The same identities in a different order are the same multiset."""
    drift = compare_identities(
        ("a::x", "a::y", "b::z"),
        ("b::z", "a::x", "a::y"),
    )

    assert drift.is_empty
    assert not drift.missing
    assert not drift.duplicate
    assert not drift.unexpected


def test_a_missing_identity_is_reported_once_per_occurrence() -> None:
    """An expected identity that never ran is a missing occurrence."""
    drift = compare_identities(("a::x", "a::y"), ("a::x",))

    assert drift.missing == ("a::y",)
    assert not drift.is_empty


def test_a_duplicate_execution_is_reported_as_duplicate_and_unexpected() -> None:
    """A second execution exceeds the expected multiset in two ways."""
    drift = compare_identities(("a::x",), ("a::x", "a::x"))

    assert drift.duplicate == ("a::x",)
    assert drift.unexpected == ("a::x",)
    assert not drift.missing


def test_an_unexpected_identity_is_reported() -> None:
    """Executing something the inventory never named fails accounting."""
    drift = compare_identities(("a::x",), ("a::x", "a::surprise"))

    assert drift.unexpected == ("a::surprise",)
    assert not drift.missing


def test_a_missing_occurrence_of_a_repeated_expectation_is_reported() -> None:
    """A repeated expectation needs both occurrences to be executed."""
    drift = compare_identities(("a::x", "a::x"), ("a::x",))

    assert drift.missing == ("a::x",)
    assert not drift.duplicate


def test_drift_lists_are_deterministic() -> None:
    """The same drift renders identically everywhere."""
    first = compare_identities(("a::b", "a::a", "c::c"), ("z::z", "y::y", "a::a"))
    second = compare_identities(("a::b", "a::a", "c::c"), ("z::z", "y::y", "a::a"))

    assert first == second
    assert list(first.missing) == sorted(first.missing)
    assert list(first.unexpected) == sorted(first.unexpected)


@pytest.mark.parametrize(
    ("expected", "executed"),
    [
        ((), ()),
        (("a::x",), ("a::x",)),
        (("a::x", "a::y"), ("a::y",)),
    ],
)
def test_comparison_is_total(
    expected: tuple[str, ...], executed: tuple[str, ...]
) -> None:
    """The comparison always returns a drift value, including for empties."""
    drift = compare_identities(expected, executed)

    assert isinstance(drift.missing, tuple)
    assert isinstance(drift.duplicate, tuple)
    assert isinstance(drift.unexpected, tuple)


def test_a_repeated_expectation_matched_exactly_has_no_drift() -> None:
    """Equal multisets have no drift even when an identity is repeated."""
    drift = compare_identities(("a::x", "a::x"), ("a::x", "a::x"))

    assert drift.is_empty
