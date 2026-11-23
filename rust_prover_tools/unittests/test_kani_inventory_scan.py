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
        ("harnesses::failing", False),
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
    assert _proofs("beta/tests/integration.rs") == [
        ("integration_proof", False),
        ("gated_proof", True),
    ]


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


def test_a_parameterized_contract_proof_is_recognised(tmp_path: Path) -> None:
    """A contract proof carries its contracted function as an argument."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[kani::proof_for_contract(check)]\nfn contract() {}\n",
        encoding="utf-8",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "contract"
    ]


def test_whitespace_inside_an_attribute_path_is_tolerated(tmp_path: Path) -> None:
    """Attributes are token sequences, not fixed spellings."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("#[kani :: proof]\nfn spaced() {}\n", encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["spaced"]


def test_a_raw_identifier_is_normalised(tmp_path: Path) -> None:
    """The r# prefix is not part of a function's name."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("#[kani::proof]\nfn r#match() {}\n", encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["match"]


def test_a_unicode_identifier_is_recognised(tmp_path: Path) -> None:
    """Rust identifiers are Unicode, not ASCII-only."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("#[kani::proof]\nfn vérifier() {}\n", encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "vérifier"
    ]


def test_a_raw_c_string_is_masked(tmp_path: Path) -> None:
    """A raw C string cannot expose a proof that is only its text."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        'const C: &CStr = cr#"text " ; #[kani::proof] fn fake() {} "#;\n'
        "#[kani::proof]\nfn real() {}\n",
        encoding="utf-8",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["real"]


def test_an_explicit_path_is_relative_to_the_source_directory(
    tmp_path: Path,
) -> None:
    """`#[path]` in foo.rs resolves beside foo.rs, not under foo/."""
    (tmp_path / "src" / "foo").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text("mod foo;\n", encoding="utf-8")
    (tmp_path / "src" / "foo.rs").write_text(
        '#[path = "proofs.rs"]\nmod proofs;\nmod bar;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn via_path() {}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "foo" / "bar.rs").write_text(
        "#[kani::proof]\nfn via_child() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::proofs::via_path",
        "foo::bar::via_child",
    ]


def test_one_file_reached_through_two_paths_keeps_both_identities(
    tmp_path: Path,
) -> None:
    """A shared source file may be two modules, not a cycle."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = "shared.rs"]\nmod a;\n#[path = "shared.rs"]\nmod b;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "shared.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "a::h",
        "b::h",
    ]


def test_an_inline_path_override_roots_its_children(tmp_path: Path) -> None:
    """An inline module's `#[path]` roots the modules nested inside it."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = "thread_files"]\nmod thread {\n'
        '    #[path = "tls.rs"]\n    mod local_data;\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "thread_files").mkdir()
    (tmp_path / "src" / "thread_files" / "tls.rs").write_text(
        "#[kani::proof]\nfn tls() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "thread::local_data::tls"
    ]


def test_a_raw_path_attribute_value_is_accepted(tmp_path: Path) -> None:
    """`#[path = r"..."]` is a string value like an ordinary literal."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = r"alternate.rs"]\nmod proofs;\n', encoding="utf-8")
    (tmp_path / "src" / "alternate.rs").write_text(
        "#[kani::proof]\nfn via_raw() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::via_raw"
    ]


def test_a_comment_inside_an_attribute_is_tolerated(tmp_path: Path) -> None:
    """A block comment inside an attribute is whitespace, not a name."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[kani /* note */ :: proof]\nfn commented() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "commented"
    ]


def test_visibility_with_whitespace_before_its_restriction_is_skipped(
    tmp_path: Path,
) -> None:
    """`pub (crate)` is the same visibility as `pub(crate)`."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[kani::proof]\npub (crate) fn restricted() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "restricted"
    ]


def test_a_plain_child_of_an_inline_module_is_under_its_name(
    tmp_path: Path,
) -> None:
    """An inline module advances the directory for its plain children."""
    (tmp_path / "src" / "outer").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text("mod outer { mod proofs; }\n", encoding="utf-8")
    (tmp_path / "src" / "outer" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "outer::proofs::h"
    ]


def test_an_inline_path_in_a_non_mod_rs_file_uses_its_child_directory(
    tmp_path: Path,
) -> None:
    """`foo.rs` inline `#[path]` starts under `foo/`."""
    (tmp_path / "src" / "foo" / "inline").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text("mod foo;\n", encoding="utf-8")
    (tmp_path / "src" / "foo.rs").write_text(
        'mod inline { #[path = "proofs.rs"] mod proofs; }\n', encoding="utf-8"
    )
    (tmp_path / "src" / "foo" / "inline" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::inline::proofs::h"
    ]


def test_plain_children_of_an_inline_path_module_use_that_directory(
    tmp_path: Path,
) -> None:
    """A `#[path]` inline module is the directory for its plain children."""
    (tmp_path / "src" / "thread_files").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = "thread_files"]\nmod thread { mod baz; }\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "thread_files" / "baz.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "thread::baz::h"
    ]


def test_an_inline_path_in_a_non_mod_rs_file_keeps_the_file_parent_base(
    tmp_path: Path,
) -> None:
    """`foo.rs` resolves an inline `#[path]` from `src`, not from `src/foo`."""
    (tmp_path / "src" / "thread_files").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text("mod foo;\n", encoding="utf-8")
    (tmp_path / "src" / "foo.rs").write_text(
        '#[path = "thread_files"]\nmod thread { mod baz; }\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "thread_files" / "baz.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::thread::baz::h"
    ]


def test_a_string_valued_attribute_does_not_name_a_module_file(
    tmp_path: Path,
) -> None:
    """`#[doc = "..."]` is not a `#[path]`, even though it has a string value."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[doc = "not_a_path.rs"]\nmod proofs;\n', encoding="utf-8")
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_a_comment_inside_cfg_kani_stays_unconditional(tmp_path: Path) -> None:
    """A comment is whitespace, so `cfg(kani /* note */)` is still `cfg(kani)`."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[cfg(kani /* active under the verifier */)]\n#[kani::proof]\nfn h() {}\n",
        encoding="utf-8",
    )

    (proof,) = collect_proofs(root, tmp_path)

    assert not proof.conditional


def test_a_hashed_raw_path_value_is_accepted(tmp_path: Path) -> None:
    """`r#"..."#` is a Rust string literal with hashes around the quotes."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = r#"alternate.rs"#]\nmod proofs;\n', encoding="utf-8")
    (tmp_path / "src" / "alternate.rs").write_text(
        "#[kani::proof]\nfn via_raw() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::via_raw"
    ]


def test_an_unsupported_path_value_is_refused(tmp_path: Path) -> None:
    """A byte string cannot name a module, so it is not silently ignored."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = b"alternate.rs"]\nmod proofs;\n', encoding="utf-8")

    with pytest.raises(InventoryDiscoveryError, match="string literal"):
        collect_proofs(root, tmp_path)


def test_whitespace_between_attribute_tokens_is_tolerated(
    tmp_path: Path,
) -> None:
    """`# [ ... ]` is an attribute, while an inner group is not code."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "# ! [kani::proof]\nfn inner() {}\n# [kani::proof]\nfn outer() {}\n",
        encoding="utf-8",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["outer"]


def test_a_proof_nested_in_cfg_attr_is_discovered(tmp_path: Path) -> None:
    """`cfg_attr` expands its attribute when the predicate holds."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[cfg_attr(kani, kani::proof)]\nfn gated() {}\n"
        '#[cfg_attr(feature = "extra", kani::proof)]\nfn later() {}\n',
        encoding="utf-8",
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("gated", False), ("later", True)]


def test_a_path_module_resolves_children_beside_its_file(
    tmp_path: Path,
) -> None:
    """`mod child;` in an explicitly located file resolves beside that file."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = "alternate.rs"]\nmod proofs;\n', encoding="utf-8")
    (tmp_path / "src" / "alternate.rs").write_text("mod child;\n", encoding="utf-8")
    (tmp_path / "src" / "child.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::child::h"
    ]


def test_a_relative_repository_root_still_yields_relative_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative root is normalised before source paths are derived."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    (proof,) = collect_proofs(Path("src/lib.rs"), Path())

    assert proof.source == "src/lib.rs"


def test_cfg_attr_accepts_whitespace_before_its_group(tmp_path: Path) -> None:
    """`cfg_attr (predicate, ...)` is the same attribute."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[cfg_attr (kani, kani::proof)]\nfn spaced() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["spaced"]


def test_a_nested_cfg_attr_proof_is_discovered(tmp_path: Path) -> None:
    """An all-`kani` chain is unconditional; a foreign predicate is not."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[cfg_attr(kani, cfg_attr(kani, kani::proof))]\nfn nested() {}\n"
        '#[cfg_attr(feature = "x", cfg_attr(kani, kani::proof))]\n'
        "fn gated() {}\n",
        encoding="utf-8",
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("nested", False), ("gated", True)]


def test_a_path_string_continuation_drops_indentation(tmp_path: Path) -> None:
    """A backslash-newline continuation removes the following whitespace."""
    (tmp_path / "src" / "sub").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = "sub/\\\n    proofs.rs"]\nmod proofs;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "sub" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_a_unicode_escape_with_underscores_names_the_file(tmp_path: Path) -> None:
    r"""Rust permits separators inside a `\u{...}` escape."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = "\\u{70_72}oofs.rs"]\nmod proofs;\n', encoding="utf-8")
    (tmp_path / "src" / "灲oofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_comments_around_a_path_value_are_whitespace(tmp_path: Path) -> None:
    """A comment before or after the literal does not change the path."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = /* before */ "proofs.rs" /* after */]\nmod proofs;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_inner_cfg_applies_to_every_sibling_proof(tmp_path: Path) -> None:
    """An inner `cfg` configures the module, not only the next item."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "mod proofs {\n"
        '    #![cfg(feature = "proofs")]\n'
        "    #[kani::proof] fn first() {}\n"
        "    #[kani::proof] fn second() {}\n"
        "}\n",
        encoding="utf-8",
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("proofs::first", True), ("proofs::second", True)]


def test_a_cfg_attr_path_selects_the_module_file(tmp_path: Path) -> None:
    """An active `cfg_attr` path overrides the conventional module file."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[cfg_attr(kani, path = "kani_proofs.rs")]\nmod proofs;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn wrong() {}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "kani_proofs.rs").write_text(
        "#[kani::proof]\nfn right() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::right"
    ]


def test_a_combining_mark_stays_in_the_identifier(tmp_path: Path) -> None:
    """A Rust identifier is normalized to NFC without losing its mark."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("#[kani::proof]\nfn cafe\u0301() {}\n", encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["café"]


def test_a_comment_before_cfg_attr_keeps_path_offsets(tmp_path: Path) -> None:
    """Comment masking must not shift the original attribute slice."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[/* note */ cfg_attr(kani, path = "chosen.rs")]\nmod proofs;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "chosen.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_trailing_space_after_nested_cfg_attr_is_tolerated(
    tmp_path: Path,
) -> None:
    """The closing parenthesis is located, not assumed to be last."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        "#[cfg_attr(kani, cfg_attr(kani, kani::proof) )]\nfn h() {}\n",
        encoding="utf-8",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["h"]


def test_a_conditional_path_is_refused(tmp_path: Path) -> None:
    """A path behind an unevaluated predicate cannot silently use the default."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[cfg_attr(feature = "x", path = "alternate.rs")]\nmod proofs;\n',
        encoding="utf-8",
    )

    with pytest.raises(InventoryDiscoveryError, match="conditional module"):
        collect_proofs(root, tmp_path)


def test_a_symlinked_path_module_resolves_children_lexically(
    tmp_path: Path,
) -> None:
    """A symlink target does not move the child directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "outside").mkdir()
    target = tmp_path / "outside" / "proofs.rs"
    target.write_text("mod child;\n", encoding="utf-8")
    (tmp_path / "src" / "proofs.rs").symlink_to(target)
    (tmp_path / "src" / "child.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = "proofs.rs"]\nmod proofs;\n', encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::child::h"
    ]


def test_a_raw_path_followed_by_a_comment_is_accepted(tmp_path: Path) -> None:
    """The raw literal ends at its own delimiter, not a later quote."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text(
        '#[path = r"proofs.rs" /* "shared" */]\nmod proofs;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_an_absolute_kani_attribute_is_a_proof(tmp_path: Path) -> None:
    """`::kani::proof` names the same path from the crate root."""
    (tmp_path / "src").mkdir()
    root = tmp_path / "src" / "lib.rs"
    root.write_text("#[::kani::proof]\nfn absolute() {}\n", encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "absolute"
    ]
