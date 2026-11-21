"""Hold the lexical scanner and module walker to the fixture workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from rust_prover_tools.kani_inventory import (
    InventoryDiscoveryError,
    collect_proofs,
)
from rust_prover_tools.kani_inventory.masking import mask_source

_WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


def _proofs(relative: str) -> list[tuple[str, bool]]:
    """Return ``(pretty name, conditional)`` pairs for one crate root."""
    return [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(_WORKSPACE / relative, _WORKSPACE)
    ]


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


def test_alpha_proofs_include_path_and_inline_modules() -> None:
    """A `#[path]` module and an inline module both extend the module path."""
    assert _proofs("alpha/src/lib.rs") == [
        ("harnesses::verified", False),
        ("harnesses::bounded", False),
        ("harnesses::conditional_extra", True),
        ("harnesses::nested::proof", False),
        ("path_proofs::via_path", False),
        ("inline::inside", False),
        ("shared::proof", False),
    ]


def test_a_binary_walks_its_own_module_tree() -> None:
    """A bin crate root resolves `mod` beside its source file."""
    assert _proofs("beta/src/main.rs") == [
        ("in_main", False),
        ("helper::in_helper", False),
    ]


def test_an_integration_test_is_its_own_crate_root() -> None:
    """A test target starts at an empty module path."""
    assert _proofs("beta/tests/integration.rs") == [("integration_proof", False)]


def test_a_proof_records_its_source_file_and_line() -> None:
    """The inventory names where each proof was found."""
    proofs = collect_proofs(_WORKSPACE / "alpha/src/lib.rs", _WORKSPACE)
    via_path = next(proof for proof in proofs if proof.function == "via_path")

    assert via_path.source == "alpha/src/extra/path_proofs.rs"
    assert via_path.line >= 1
    assert via_path.attributes == ("kani::proof",)


def test_an_ambiguous_module_file_is_refused() -> None:
    """Rust itself rejects both `thing.rs` and `thing/mod.rs`."""
    with pytest.raises(InventoryDiscoveryError, match="ambiguous"):
        collect_proofs(_WORKSPACE / "ambiguous/src/lib.rs", _WORKSPACE)


def test_a_missing_module_file_is_refused(tmp_path: Path) -> None:
    """A declaration with no file cannot yield selectors."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("mod missing;\n", encoding="utf-8")

    with pytest.raises(InventoryDiscoveryError, match="has no file"):
        collect_proofs(root, tmp_path)


def test_a_missing_path_attribute_file_is_refused(tmp_path: Path) -> None:
    """A `#[path]` registration that does not exist is an error."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = "gone.rs"]\nmod gone;\n', encoding="utf-8")

    with pytest.raises(InventoryDiscoveryError, match="does not exist"):
        collect_proofs(root, tmp_path)


def test_a_module_cycle_is_refused(tmp_path: Path) -> None:
    """A file reachable twice would produce duplicate selectors."""
    (tmp_path / "src").mkdir()
    first = tmp_path / "src" / "a.rs"
    second = tmp_path / "src" / "b.rs"
    first.write_text('#[path = "b.rs"]\nmod b;\n', encoding="utf-8")
    second.write_text('#[path = "a.rs"]\nmod a;\n', encoding="utf-8")

    with pytest.raises(InventoryDiscoveryError, match=r"cycle|twice"):
        collect_proofs(first, tmp_path)


def test_a_non_kani_cfg_propagates_to_nested_inline_proofs(tmp_path: Path) -> None:
    """A gated file marks every proof it contains as conditional."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[cfg(feature = "x")]\nmod gated;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "gated.rs").write_text(
        "pub mod inner {\n    #[kani::proof]\n    fn nested() {}\n}\n",
        encoding="utf-8",
    )

    proofs = collect_proofs(root, tmp_path)

    assert [(proof.pretty_name, proof.conditional) for proof in proofs] == [
        ("gated::inner::nested", True)
    ]
