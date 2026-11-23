"""Prove matcher and macro-scope tokens are read as the compiler reads them."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, scan
from rust_prover_tools.unittests.trust_audit_scan_support import items


def test_a_trust_use_in_a_matcher_is_not_reported() -> None:
    """A matcher describes the input it accepts; it requests nothing."""
    source = "macro_rules! m { (kani::assume) => {}; }"

    assert items(source) == [], "a matcher spelling is not a trust use"


def test_a_stringified_macro_definition_does_not_collide() -> None:
    """A definition spelled inside ``stringify!`` is text, not a definition."""
    source = "\n".join(
        (
            "const S: &str = stringify!(macro_rules! emit { () => {}; });",
            "macro_rules! emit { () => {}; }",
            "fn f() { emit!(); }",
        ),
    )

    assert not scan(source).items, "a stringified definition defines no macro"


def test_a_stringified_import_is_not_an_opaque_alias() -> None:
    """An import spelled inside ``stringify!`` binds no local macro name."""
    source = "\n".join(
        (
            "const S: &str = stringify! { use std::include as noop; };",
            "macro_rules! noop { () => {}; }",
            "fn f() { noop!(); }",
        ),
    )

    assert not scan(source).items, "a stringified import binds no alias"


def test_an_import_rename_after_a_sibling_repetition_is_refused() -> None:
    """Trace ``$item as target`` past a repetition that precedes it."""
    source = "\n".join(
        (
            "macro_rules! bind {",
            "    ($($extra:ident,)* ; $item:ident) => {",
            "        use std::{ $($extra,)* $item as target };",
            "    };",
            "}",
            "bind!(fmt, ; include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_nested_use_group_is_traced_to_its_import() -> None:
    """Walk through a brace that follows a comma inside a use tree."""
    source = "\n".join(
        (
            "macro_rules! m {",
            "    ($name:ident) => { use {other, {std::$name as load}}; };",
            "}",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_forwarded_arguments_are_followed_through_a_macro_chain() -> None:
    """Refuse ``wrap!(relay!())`` when ``relay!`` reaches ``mod child;``."""
    source = "\n".join(
        (
            "macro_rules! leaf { () => { mod child; }; }",
            "macro_rules! relay { () => { leaf!(); }; }",
            "macro_rules! wrap { ($($t:tt)*) => { mod nested { $($t)* } }; }",
            "mod holder { wrap!(relay!()); }",
        ),
    )

    with pytest.raises(TrustAuditError, match="passes a module declaration"):
        items(source)


def test_a_cfg_attr_predicate_named_macro_use_is_not_imported() -> None:
    """``cfg_attr(macro_use, …)`` states a predicate, not an attribute."""
    source = "\n".join(
        (
            "#[cfg_attr(macro_use, allow(dead_code))]",
            'fn f() { println!("not a module macro"); }',
        ),
    )

    assert not scan(source).items, "the configuration predicate imports no macro"


def test_a_cfg_attr_payload_carrying_macro_use_is_imported() -> None:
    """Refuse an imported macro whose payload applies ``macro_use``."""
    source = "\n".join(
        (
            "#[cfg_attr(unix, macro_use)]",
            "mod macros;",
            'fn f() { println!("x"); }',
        ),
    )

    with pytest.raises(TrustAuditError, match="#\\[macro_use\\]"):
        items(source)


@pytest.mark.parametrize(
    "statement",
    ["return ::kani::$($f)+(true);", "for _ in ::kani::$($f)+(true) {}"],
)
def test_a_keyword_before_an_absolute_trust_path_is_refused(statement: str) -> None:
    """Skip the keyword so the root is the crate name after the leading ``::``."""
    source = f"macro_rules! call {{ ($($f:ident)+) => {{ {statement} }}; }}"

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_glob_import_that_can_bind_stringify_is_refused() -> None:
    """A glob may bind a user ``stringify``, so the argument is not text."""
    source = "use generated::*;\nconst S: &str = stringify!(mod hidden;);"

    with pytest.raises(TrustAuditError, match="glob import"):
        items(source)


def test_a_glob_import_without_stringify_is_accepted() -> None:
    """A glob alone does not make an unrelated file unreadable."""
    source = "use generated::*;\nfn f() {}\n"

    assert not scan(source).items, "an unused glob imports no trust item"


def test_a_shadowed_std_path_does_not_exempt_stringify() -> None:
    """A local ``std`` binding makes ``std::stringify!`` a user macro."""
    source = "use other::nested as std;\nconst S: &str = std::stringify!(mod hidden;);"

    assert [module.name for module in scan(source).modules] == ["hidden"], (
        "a shadowed std must not exempt the argument from module discovery"
    )
