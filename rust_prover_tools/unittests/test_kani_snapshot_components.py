"""Tests for the canonical rustup component-name digest.

Rustup writes `lib/rustlib/components` in component-completion order, which is
not deterministic: identical installs produce the same names in different
orders. The manifest therefore records a digest of the sorted names, and these
tests pin that order is irrelevant while any change to the set is detected.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.toolchain_model import (
    MAX_COMPONENTS_BYTES,
    canonical_component_names,
    components_digest,
)

_NAMES = st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True)


def test_component_order_does_not_change_the_digest() -> None:
    """Two real rustup orders for the same set have one digest."""
    first = b"rustc\ncargo\nrust-std\n"
    second = b"rust-std\nrustc\ncargo\n"

    assert components_digest(first) == components_digest(second)
    assert canonical_component_names(first) == b"cargo\nrust-std\nrustc\n"


def test_an_added_component_changes_the_digest() -> None:
    """A component the pin did not record is detected."""
    base = b"cargo\nrustc\n"

    assert components_digest(base) != components_digest(base + b"rust-docs\n")


def test_a_removed_component_changes_the_digest() -> None:
    """A recorded component that is missing is detected."""
    base = b"cargo\nrustc\n"

    assert components_digest(base) != components_digest(b"cargo\n")


def test_a_renamed_component_changes_the_digest() -> None:
    """A renamed component is a different set, not the same one."""
    assert components_digest(b"rustc\n") != components_digest(b"rustc-dev\n")


def test_whitespace_and_blank_lines_are_normalised() -> None:
    """Trailing spaces, CRLF and blank lines do not alter the set."""
    text = b"\r\ncargo \r\n\r\nrustc\r\n"

    assert canonical_component_names(text) == b"cargo\nrustc\n"


def test_a_duplicate_component_is_refused() -> None:
    """A name listed twice is ambiguous, not a set."""
    with pytest.raises(ProverToolError, match="more than once"):
        canonical_component_names(b"cargo\nrustc\ncargo\n")


def test_an_empty_record_is_refused() -> None:
    """A toolchain with no components is not a valid record."""
    with pytest.raises(ProverToolError, match="no components"):
        canonical_component_names(b"\n  \n")


def test_an_oversized_record_is_refused() -> None:
    """The reader bounds a file it parses."""
    with pytest.raises(ProverToolError, match="ceiling"):
        canonical_component_names(b"x" * (MAX_COMPONENTS_BYTES + 1))


def test_invalid_utf8_is_refused() -> None:
    """A component list is UTF-8 text, and anything else fails closed."""
    with pytest.raises(ProverToolError, match="UTF-8"):
        canonical_component_names(b"\xff\xfe")


@given(names=st.sets(_NAMES, min_size=1, max_size=12))
@settings(max_examples=40, deadline=None)
def test_every_permutation_has_one_digest(names: set[str]) -> None:
    """Generated component sets hash identically in any written order."""
    ordered = "".join(f"{name}\n" for name in sorted(names)).encode()
    reversed_order = "".join(f"{name}\n" for name in sorted(names, reverse=True))
    shuffled = reversed_order.encode()

    assert components_digest(ordered) == components_digest(shuffled)
    assert canonical_component_names(shuffled) == ordered
