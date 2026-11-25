"""Tests for the canonical rustup component-name digest.

Rustup writes `lib/rustlib/components` in component-completion order, which is
not deterministic: identical installs produce the same names in different
orders. The manifest therefore records a digest of the sorted names, and these
tests pin that order is irrelevant while any change to the set is detected.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.toolchain_model import (
    MAX_COMPONENTS_BYTES,
    canonical_component_names,
    components_digest,
    components_digest_of_file,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

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


def test_a_component_file_is_read_and_digested_in_one_query(tmp_path: Path) -> None:
    """The shared file query returns the same digest as the byte query."""
    components = tmp_path / "components"
    components.write_bytes(b"rust-std\ncargo\nrustc\n")

    assert components_digest_of_file(components) == components_digest(
        b"rustc\ncargo\nrust-std\n"
    )


def test_a_missing_component_file_is_reported_as_a_read_failure(
    tmp_path: Path,
) -> None:
    """The producer and the consumer share one failure text for a read error."""
    with pytest.raises(ProverToolError, match="failed to read rustup components list"):
        components_digest_of_file(tmp_path / "absent")


def test_an_oversized_component_file_is_refused(tmp_path: Path) -> None:
    """The bounded read leaves the ceiling check to the digest it calls."""
    components = tmp_path / "components"
    components.write_bytes(b"x" * (MAX_COMPONENTS_BYTES + 1))

    with pytest.raises(ProverToolError, match="ceiling"):
        components_digest_of_file(components)


@given(names=st.sets(_NAMES, min_size=1, max_size=12), data=st.data())
@settings(max_examples=40, deadline=None)
def test_any_written_order_has_the_sorted_digest(
    names: set[str], data: st.DataObject
) -> None:
    """Any order has the digest of the sorted names, not just two of them.

    An earlier version checked ascending and descending order only, which
    proves nothing about a set with more than two orders. This shuffles the
    names independently for each example and compares each result with the
    sorted form.
    """
    ordered = "".join(f"{name}\n" for name in sorted(names)).encode()
    shuffled_names = data.draw(st.permutations(sorted(names)))
    shuffled = "".join(f"{name}\n" for name in shuffled_names).encode()

    assert canonical_component_names(shuffled) == ordered
    assert components_digest(shuffled) == components_digest(ordered)
