"""Hold attribute parsing and cfg_attr folding to their contract."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_inventory import (
    InventoryDiscoveryError,
    collect_proofs,
)
from rust_prover_tools.kani_inventory.attribute_values import cfg_attr_facts
from rust_prover_tools.unittests.kani_inventory_scan_support import (
    scan as _scan,
)
from rust_prover_tools.unittests.kani_inventory_scan_support import write_lib

if typ.TYPE_CHECKING:
    from pathlib import Path

#: Nested attributes a ``cfg_attr`` can carry with their expected facts.
_CFG_ATTR_NESTED = (
    ("kani::proof", ("kani::proof",), False),
    ("allow(dead_code)", (), False),
    ('cfg(feature = "proofs")', (), True),
    ("cfg(kani)", (), False),
)


@given(
    predicate=st.sampled_from(("kani", 'feature = "proofs"', "not(kani)")),
    nested=st.sampled_from(_CFG_ATTR_NESTED),
)
@settings(max_examples=50, deadline=None)
def test_cfg_attr_conditionality_follows_its_nested_attribute(
    predicate: str,
    nested: tuple[str, tuple[str, ...], bool],
) -> None:
    """A generated cfg_attr folds to the facts its nested attribute carries."""
    attribute, expected_proofs, gating = nested
    source = f"cfg_attr({predicate}, {attribute})"

    facts = cfg_attr_facts(source, source)

    assert facts is not None
    proofs, conditional, path = facts
    assert proofs == list(expected_proofs)
    assert path is None
    is_proof = "kani::proof" in expected_proofs
    assert conditional == (gating or (is_proof and predicate != "kani"))


def test_a_parameterized_contract_proof_is_recognised(tmp_path: Path) -> None:
    """A contract proof carries its contracted function as an argument."""
    root = write_lib(
        tmp_path,
        "#[kani::proof_for_contract(check)]\nfn contract() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "contract"
    ]


def test_whitespace_inside_an_attribute_path_is_tolerated(tmp_path: Path) -> None:
    """Attributes are token sequences, not fixed spellings."""
    root = write_lib(tmp_path, "#[kani :: proof]\nfn spaced() {}\n")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["spaced"]


def test_a_raw_path_attribute_value_is_accepted(tmp_path: Path) -> None:
    """`#[path = r"..."]` is a string value like an ordinary literal."""
    root = write_lib(tmp_path, '#[path = r"alternate.rs"]\nmod proofs;\n')
    (tmp_path / "src" / "alternate.rs").write_text(
        "#[kani::proof]\nfn via_raw() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::via_raw"
    ]


def test_a_comment_inside_an_attribute_is_tolerated(tmp_path: Path) -> None:
    """A block comment inside an attribute is whitespace, not a name."""
    root = write_lib(
        tmp_path,
        "#[kani /* note */ :: proof]\nfn commented() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "commented"
    ]


def test_a_string_valued_attribute_does_not_name_a_module_file(
    tmp_path: Path,
) -> None:
    """`#[doc = "..."]` is not a `#[path]`, even though it has a string value."""
    root = write_lib(tmp_path, '#[doc = "not_a_path.rs"]\nmod proofs;\n')
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_a_comment_inside_cfg_kani_stays_unconditional(tmp_path: Path) -> None:
    """A comment is whitespace, so `cfg(kani /* note */)` is still `cfg(kani)`."""
    root = write_lib(
        tmp_path,
        "#[cfg(kani /* active under the verifier */)]\n#[kani::proof]\nfn h() {}\n",
    )

    (proof,) = collect_proofs(root, tmp_path)

    assert not proof.conditional


def test_a_hashed_raw_path_value_is_accepted(tmp_path: Path) -> None:
    """`r#"..."#` is a Rust string literal with hashes around the quotes."""
    root = write_lib(tmp_path, '#[path = r#"alternate.rs"#]\nmod proofs;\n')
    (tmp_path / "src" / "alternate.rs").write_text(
        "#[kani::proof]\nfn via_raw() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::via_raw"
    ]


def test_an_unsupported_path_value_is_refused(tmp_path: Path) -> None:
    """A byte string cannot name a module, so it is not silently ignored."""
    root = write_lib(tmp_path, '#[path = b"alternate.rs"]\nmod proofs;\n')

    with pytest.raises(InventoryDiscoveryError, match="string literal"):
        collect_proofs(root, tmp_path)


def test_whitespace_between_attribute_tokens_is_tolerated(
    tmp_path: Path,
) -> None:
    """`# [ ... ]` is an attribute, while an inner group is not code."""
    root = write_lib(
        tmp_path,
        "# ! [kani::proof]\nfn inner() {}\n# [kani::proof]\nfn outer() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["outer"]


def test_a_proof_nested_in_cfg_attr_is_discovered(tmp_path: Path) -> None:
    """`cfg_attr` expands its attribute when the predicate holds."""
    root = write_lib(
        tmp_path,
        "#[cfg_attr(kani, kani::proof)]\nfn gated() {}\n"
        '#[cfg_attr(feature = "extra", kani::proof)]\nfn later() {}\n',
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("gated", False), ("later", True)]


def test_cfg_attr_accepts_whitespace_before_its_group(tmp_path: Path) -> None:
    """`cfg_attr (predicate, ...)` is the same attribute."""
    root = write_lib(
        tmp_path,
        "#[cfg_attr (kani, kani::proof)]\nfn spaced() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["spaced"]


def test_a_nested_cfg_attr_proof_is_discovered(tmp_path: Path) -> None:
    """An all-`kani` chain is unconditional; a foreign predicate is not."""
    root = write_lib(
        tmp_path,
        "#[cfg_attr(kani, cfg_attr(kani, kani::proof))]\nfn nested() {}\n"
        '#[cfg_attr(feature = "x", cfg_attr(kani, kani::proof))]\n'
        "fn gated() {}\n",
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("nested", False), ("gated", True)]


def test_a_path_string_continuation_drops_indentation(tmp_path: Path) -> None:
    """A backslash-newline continuation removes the following whitespace."""
    (tmp_path / "src" / "sub").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(
        tmp_path,
        '#[path = "sub/\\\n    proofs.rs"]\nmod proofs;\n',
    )
    (tmp_path / "src" / "sub" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_a_unicode_escape_with_underscores_names_the_file(tmp_path: Path) -> None:
    r"""Rust permits separators inside a `\u{...}` escape."""
    root = write_lib(tmp_path, '#[path = "\\u{70_72}oofs.rs"]\nmod proofs;\n')
    (tmp_path / "src" / "灲oofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_comments_around_a_path_value_are_whitespace(tmp_path: Path) -> None:
    """A comment before or after the literal does not change the path."""
    root = write_lib(
        tmp_path,
        '#[path = /* before */ "proofs.rs" /* after */]\nmod proofs;\n',
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_a_cfg_attr_path_selects_the_module_file(tmp_path: Path) -> None:
    """An active `cfg_attr` path overrides the conventional module file."""
    root = write_lib(
        tmp_path,
        '#[cfg_attr(kani, path = "kani_proofs.rs")]\nmod proofs;\n',
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


def test_a_comment_before_cfg_attr_keeps_path_offsets(tmp_path: Path) -> None:
    """Comment masking must not shift the original attribute slice."""
    root = write_lib(
        tmp_path,
        '#[/* note */ cfg_attr(kani, path = "chosen.rs")]\nmod proofs;\n',
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
    root = write_lib(
        tmp_path,
        "#[cfg_attr(kani, cfg_attr(kani, kani::proof) )]\nfn h() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == ["h"]


def test_a_conditional_path_is_refused(tmp_path: Path) -> None:
    """A path behind an unevaluated predicate cannot silently use the default."""
    root = write_lib(
        tmp_path,
        '#[cfg_attr(feature = "x", path = "alternate.rs")]\nmod proofs;\n',
    )

    with pytest.raises(InventoryDiscoveryError, match="conditional module"):
        collect_proofs(root, tmp_path)


def test_a_raw_path_followed_by_a_comment_is_accepted(tmp_path: Path) -> None:
    """The raw literal ends at its own delimiter, not a later quote."""
    root = write_lib(
        tmp_path,
        '#[path = r"proofs.rs" /* "shared" */]\nmod proofs;\n',
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::h"
    ]


def test_an_absolute_kani_attribute_is_a_proof(tmp_path: Path) -> None:
    """`::kani::proof` names the same path from the crate root."""
    root = write_lib(tmp_path, "#[::kani::proof]\nfn absolute() {}\n")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "absolute"
    ]


def test_a_lint_only_cfg_attr_does_not_make_a_proof_conditional() -> None:
    """Only attributes that can gate the item make it conditional.

    `cfg_attr(feature = "strict", allow(dead_code))` applies a lint attribute
    when the feature holds; it never removes the proof, so the harness stays
    unconditional.
    """
    attribute = 'cfg_attr(feature = "strict", allow(dead_code))'
    facts = cfg_attr_facts(attribute, attribute)

    assert facts is not None
    assert facts[1] is False


def test_a_cfg_attr_wrapping_a_proof_stays_conditional() -> None:
    """A proof nested under a feature predicate can be absent."""
    attribute = 'cfg_attr(feature = "proofs", kani::proof)'
    facts = cfg_attr_facts(attribute, attribute)

    assert facts is not None
    assert facts[1] is True
    assert facts[0] == ["kani::proof"]


def test_a_kani_cfg_attr_wrapping_a_feature_cfg_stays_conditional() -> None:
    """Under Kani the outer predicate holds, so the nested cfg gates the item."""
    attribute = 'cfg_attr(kani, cfg(feature = "proofs"))'
    facts = cfg_attr_facts(attribute, attribute)

    assert facts is not None
    assert facts[1] is True


def test_an_inner_cfg_attr_gate_reaches_the_nested_proof(tmp_path: Path) -> None:
    """An inner cfg_attr that gates the module gates its proofs too."""
    body = '#![cfg_attr(kani, cfg(feature = "proofs"))]\n#[kani::proof]\nfn h() {}\n'

    assert _scan(tmp_path, body) == [("h", True)]


def test_a_lint_only_inner_cfg_attr_leaves_proofs_unconditional(
    tmp_path: Path,
) -> None:
    """An inner lint-only wrapper does not gate the module."""
    body = (
        '#![cfg_attr(feature = "strict", allow(dead_code))]\n'
        "#[kani::proof]\n"
        "fn h() {}\n"
    )

    assert _scan(tmp_path, body) == [("h", False)]
