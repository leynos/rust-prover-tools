"""Hold source masking and Rust identifier reading to their contract."""

from __future__ import annotations

import typing as typ

from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_inventory import collect_proofs
from rust_prover_tools.kani_inventory.masking import Identifier, identifier, mask_source
from rust_prover_tools.unittests.kani_inventory_scan_support import (
    scan as _scan,
)
from rust_prover_tools.unittests.kani_inventory_scan_support import write_lib

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_masking_blanks_comments_and_strings_but_keeps_layout() -> None:
    """A proof or module name inside a comment or string is not code."""
    text = (
        "// mod fake;\n"
        "/* mod nested_fake; */\n"
        'let literal = "mod string_fake;";\n'
        'let raw = r#"#[kani::proof] fn raw_fake() {}"#;\n'
        "mod real;\n"
    )

    masked = mask_source(text)

    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")
    assert "fake" not in masked
    assert "mod real" in masked


@given(st.text())
@settings(max_examples=100, deadline=None)
def test_masking_preserves_length_and_newline_positions(text: str) -> None:
    """Masked source keeps every position, so line numbers describe it.

    The contract is structural: comments and literals become spaces, but no
    character is added or removed, and every newline stays where it was. That
    makes a reported location in the masked text a location in the original.
    """
    masked = mask_source(text)

    assert len(masked) == len(text)
    assert [index for index, char in enumerate(masked) if char == "\n"] == [
        index for index, char in enumerate(text) if char == "\n"
    ]


@given(st.text())
@settings(max_examples=100, deadline=None)
def test_masking_is_idempotent(text: str) -> None:
    """Masked source contains no comment or literal the scan would mask again."""
    once = mask_source(text)

    assert mask_source(once) == once


@given(st.text(alphabet="abc_", min_size=1, max_size=12))
@settings(max_examples=50, deadline=None)
def test_an_identifier_is_read_whole_and_a_raw_prefix_is_normalized(
    name: str,
) -> None:
    """Every generated identifier is read exactly, with or without ``r#``."""
    plain = identifier(name, 0)

    assert plain == Identifier(name, len(name), is_raw=False)
    raw = identifier("r#" + name, 0)
    assert raw is not None
    assert raw.text == name
    assert raw.end == len(name) + 2
    assert raw.is_raw is True


def test_masking_keeps_lifetimes_and_masks_character_literals() -> None:
    """A lifetime is code; a character literal is data."""
    text = (
        "fn borrow<'a>(value: &'a u8) -> char { '\\n' }\n"
        "#[kani::proof]\nfn proof() {}\n"
    )

    masked = mask_source(text)

    assert "'a" in masked
    assert "#[kani::proof]" in masked
    assert "\\n" not in masked


def test_a_raw_identifier_is_normalised(tmp_path: Path) -> None:
    """The r# prefix is not part of a function's name."""
    root = write_lib(tmp_path, "#[kani::proof]\nfn r#match() {}\n")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["match"]


def test_a_unicode_identifier_is_recognised(tmp_path: Path) -> None:
    """Rust identifiers are Unicode, not ASCII-only."""
    root = write_lib(tmp_path, "#[kani::proof]\nfn vérifier() {}\n")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "vérifier"
    ]


def test_a_raw_c_string_is_masked(tmp_path: Path) -> None:
    """A raw C string cannot expose a proof that is only its text."""
    root = write_lib(
        tmp_path,
        'const C: &CStr = cr#"text " ; #[kani::proof] fn fake() {} "#;\n'
        "#[kani::proof]\nfn real() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["real"]


def test_a_combining_mark_stays_in_the_identifier(tmp_path: Path) -> None:
    """A Rust identifier is normalized to NFC without losing its mark."""
    root = write_lib(tmp_path, "#[kani::proof]\nfn cafe\u0301() {}\n")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["café"]


def test_xid_identifiers_are_discovered(tmp_path: Path) -> None:
    """Rust accepts XID names that alphabetic categories alone miss."""
    body = "#[kani::proof]\nfn ℘() {}\n#[kani::proof]\nfn a·b() {}\n"

    assert _scan(tmp_path, body) == [("℘", False), ("a·b", False)]
