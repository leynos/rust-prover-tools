"""Prove the trust scan refuses a macro whose expansion it cannot read."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, scan
from rust_prover_tools.unittests.trust_audit_scan_support import items


def test_an_opaque_macro_is_refused() -> None:
    """Refuse a macro that generates code the scan never sees."""
    with pytest.raises(TrustAuditError, match=r"include! at line 1, column 1"):
        scan('include!("generated.rs");')


def test_a_renamed_opaque_macro_is_refused() -> None:
    """Follow a ``use`` rename so ``load!`` is still ``include!``."""
    source = 'use std::include as load;\nload!("generated.rs");'
    with pytest.raises(TrustAuditError, match=r"load! at line 2, column 1"):
        scan(source)


@pytest.mark.parametrize(
    "source",
    [
        'fn f() { let _ = include_str!("notes.txt"); }',
        'use std::include_str as load;\nfn f() { let _ = load!("notes.txt"); }',
    ],
)
def test_include_str_is_not_opaque_for_an_ordinary_crate(source: str) -> None:
    """Leave the splice alone where a string literal is not read as code."""
    assert items(source) == []


@pytest.mark.parametrize(
    ("source", "pattern"),
    [
        (
            'fn f() { let _ = include_str!("notes.txt"); }',
            r"include_str! at line 1, column 18",
        ),
        (
            'use std::include_str as load;\nload!("notes.txt");',
            r"load! at line 2, column 1",
        ),
    ],
)
def test_a_procedural_macro_scan_refuses_include_str(
    source: str,
    pattern: str,
) -> None:
    """Refuse a splice a proc-macro crate could expand into code.

    The expansion is a string literal, and a proc-macro crate's literals are
    read as code, so the spliced file's text would never have been lexed.
    """
    with pytest.raises(TrustAuditError, match=pattern):
        scan(source, procedural_macro=True)


def test_a_matcher_import_rename_is_not_an_opaque_alias() -> None:
    """An import written in a matcher describes input; it binds nothing."""
    source = "\n".join(
        (
            "macro_rules! m { (use std::include as load;) => {}; }",
            'load!("hidden.rs");',
        ),
    )

    assert items(source) == []


def test_an_opaque_macro_in_a_matcher_pattern_is_not_refused() -> None:
    """Read a matcher pattern as input, not as an ``include!`` invocation."""
    assert items("macro_rules! m { (include!($x:literal)) => { () }; }") == []


def test_an_opaque_macro_invocation_is_still_refused() -> None:
    """Refuse the invocation the pattern merely describes."""
    with pytest.raises(TrustAuditError, match=r"include! .* generates code"):
        scan('macro_rules! m { (include!($x:literal)) => { () }; }\ninclude!("x");')


def test_include_bytes_is_opaque_in_a_procedural_macro_crate() -> None:
    """Fail closed on a template a proc-macro crate could decode and parse."""
    source = 'fn f() { include_bytes!("template.rs"); }'

    with pytest.raises(TrustAuditError, match=r"include_bytes! .* generates code"):
        scan(source, procedural_macro=True)


def test_include_bytes_is_read_as_trivia_in_an_ordinary_crate() -> None:
    """An ordinary crate's byte array is not code the audit reads."""
    source = 'fn f() { include_bytes!("template.rs"); }'

    assert scan(source).items == ()


def test_a_procedural_macro_scan_refuses_concat() -> None:
    """Refuse ``concat!`` where literals are read as code.

    It joins adjacent literals into a value no single literal holds, so the
    scan cannot read the generated text.
    """
    source = 'fn f() { concat!("ka", "ni::ass", "ume(false);"); }'

    with pytest.raises(TrustAuditError, match="concat!"):
        scan(source, procedural_macro=True)


def test_concat_is_trivia_in_an_ordinary_crate() -> None:
    """Leave ``concat!`` alone where a literal is not read as code."""
    source = 'fn f() { let _ = concat!("ka", "ni"); }'

    assert scan(source).items == ()


def test_a_macro_defined_twice_is_refused() -> None:
    """Refuse a shadowed definition whose invocation cannot be attributed."""
    source = "\n".join(
        (
            "macro_rules! load { () => { mod first; }; }",
            "macro_rules! load { () => {}; }",
        ),
    )

    with pytest.raises(TrustAuditError, match="defines 'load' more than once"):
        items(source)


def test_a_metavariable_macro_invocation_is_refused() -> None:
    """Refuse an expansion whose invoked macro only the arguments decide."""
    source = "\n".join(
        (
            'macro_rules! invoke { ($m:ident) => { $m!("hidden.rs"); }; }',
            "invoke!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="through the metavariable"):
        items(source)


def test_a_metavariable_path_to_a_trust_item_is_refused() -> None:
    """Refuse an expansion whose path qualifier only its arguments decide."""
    source = "macro_rules! call { ($p:path) => { $p::assume(true); }; }"

    with pytest.raises(TrustAuditError, match="reaches a trust item"):
        items(source)


def test_a_macro_rules_definition_inside_a_matcher_is_not_a_definition() -> None:
    """Do not treat a matcher's literal tokens as a second definition."""
    source = "\n".join(
        (
            "macro_rules! outer { (macro_rules! x { () => {} }) => {}; }",
            "macro_rules! x { () => {} }",
        ),
    )

    assert items(source) == []


def test_a_metavariable_invocation_in_a_nested_matcher_is_accepted() -> None:
    """A matcher nested in an expansion describes input; it invokes nothing."""
    source = "\n".join(
        (
            "macro_rules! outer {",
            "    ($m:ident) => { macro_rules! inner { ($m!()) => {}; } };",
            "}",
            "outer!(noop);",
        ),
    )

    assert items(source) == []


def test_a_metavariable_import_rename_is_refused() -> None:
    """Refuse ``use std::$name as load;``, whose bound name the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! wrapper {",
            '    ($name:ident) => { use std::$name as load; load!("hidden.rs"); };',
            "}",
            "wrapper!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_rename_after_a_sibling_group_is_refused() -> None:
    """Skip a balanced sibling group rather than stopping at its closing brace."""
    source = "\n".join(
        (
            "macro_rules! bind {",
            "    ($name:ident) => { use std::{fmt::{Debug}, $name as load}; };",
            "}",
            "bind!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_import_rename_inside_a_group_is_refused() -> None:
    """Refuse ``use std::{$name as load};``, a grouped form of the same rename."""
    source = "\n".join(
        (
            "macro_rules! wrapper {",
            '    ($name:ident) => { use std::{$name as load}; load!("hidden.rs"); };',
            "}",
            "wrapper!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_glob_root_is_refused() -> None:
    """Refuse ``use $p::*;``, whose bound names only the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! glob { ($p:ident) => { use $p::*; assume(true); }; }",
            "glob!(kani);",
        ),
    )

    with pytest.raises(TrustAuditError, match="glob or grouped"):
        items(source)


def test_a_metavariable_grouped_import_root_is_refused() -> None:
    """Refuse ``use $p::{assume as a};``, whose crate the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! bind { ($p:ident) => { use $p::{assume as a}; }; }",
            "bind!(kani);",
            "fn f() { a(true); }",
        ),
    )

    with pytest.raises(TrustAuditError, match="glob or grouped"):
        items(source)


def test_a_metavariable_import_destination_is_refused() -> None:
    """Refuse ``use std::include as $n;``, whose local name is an argument."""
    source = "\n".join(
        (
            "macro_rules! bind { ($n:ident) => { use std::include as $n; }; }",
            "bind!(load);",
            'load!("hidden.rs");',
        ),
    )

    with pytest.raises(TrustAuditError, match="local name"):
        items(source)


def test_a_metavariable_cast_is_not_an_import_rename() -> None:
    """Leave ``$value as u64`` alone: it is an expression, not an import."""
    source = "\n".join(
        (
            "macro_rules! widen { ($value:expr) => { $value as u64 }; }",
            "fn f() { widen(1); }",
        ),
    )

    assert items(source) == []


def test_a_metavariable_final_path_segment_is_refused() -> None:
    """Refuse ``kani::$f(..)``, whose called item only the arguments decide."""
    source = "macro_rules! call { ($f:ident) => { kani::$f(true) }; }"

    with pytest.raises(TrustAuditError, match="final path segment"):
        items(source)


def test_a_metavariable_in_both_path_segments_is_refused() -> None:
    """Refuse ``$p::$i(..)``, whose called item and crate are both arguments."""
    source = (
        "macro_rules! call { ($p:ident, $i:ident) => { $p::$i(true); }; }\n"
        "fn f() { call!(kani, assume); }"
    )

    with pytest.raises(TrustAuditError, match="metavariables"):
        items(source)


def test_a_trust_qualified_metavariable_path_need_not_be_called() -> None:
    """Refuse ``kani::$f`` even when it names an item rather than calling one."""
    source = "macro_rules! get { ($f:ident) => { kani::$f }; }"

    with pytest.raises(TrustAuditError, match="final path segment"):
        items(source)


def test_a_repetition_wrapped_metavariable_trust_path_is_refused() -> None:
    """Refuse ``kani::$($f)*(..)``, whose item the arguments decide."""
    source = "macro_rules! call { ($f:ident) => { kani::$($f)*(true) }; }"

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_repetition_wrapped_metavariable_qualifier_is_refused() -> None:
    """Refuse ``$($p)+::assume(..)``, whose crate the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! call { ($($p:ident)+) => { $($p)+::assume(true) }; }",
            "call!(kani);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_repetition_wrapped_metavariable_invocation_is_refused() -> None:
    """Refuse ``$($m)+!("hidden.rs")``, whose macro the arguments decide."""
    source = "\n".join(
        (
            'macro_rules! load { ($($m:ident)+) => { $($m)+!("hidden.rs"); }; }',
            "load!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_nested_repetition_wrapped_trust_path_is_refused() -> None:
    """Walk every enclosing repetition before deciding what a path is rooted at."""
    source = "\n".join(
        (
            "macro_rules! call {",
            "    ($($($f:ident),+);+) => { kani::$($($f),+);+(true) };",
            "}",
            "call!(assume);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_an_ordinary_repetition_is_accepted() -> None:
    """Leave a repetition that emits no path or invocation alone.

    Only a repetition wrapped around a metavariable that then reaches a trust
    path or an invocation is refused; a plain list expansion is ordinary code.
    """
    source = "\n".join(
        (
            "macro_rules! list { ($($x:ident),*) => { $($x),* }; }",
            "fn f() { list!(a, b); }",
        ),
    )

    assert items(source) == []


def test_a_nested_repetition_with_a_call_is_refused() -> None:
    """Climb past a repetition whose next token is a call, not the group close."""
    source = (
        "macro_rules! call { "
        "($($($f:ident),+ (true))+) => { kani::$($($f),+ (true))+ }; }"
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_an_inner_repetition_invocation_is_refused() -> None:
    """Refuse an inner repetition that invokes a macro chosen by its argument.

    The outer repetition ends before a brace, so only the inner boundary names
    the ``!`` and the macro it invokes.
    """
    source = (
        'macro_rules! load { ($($($m:ident)+);*) => { $( $($m)+!("hidden.rs"); )* }; }'
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_nested_repetition_without_a_trust_path_is_accepted() -> None:
    """Leave a nested repetition that emits no trust path alone."""
    source = "macro_rules! list { ($($($x:ident),+);+) => { $($($x),+);+ }; }"

    assert items(source) == []


def test_an_opaque_macro_alias_of_an_alias_is_refused() -> None:
    """Follow a bare rename of the opaque macro name to its second spelling."""
    source = "\n".join(
        (
            "use std::include as load;",
            "use load as hidden;",
            'hidden!("hidden.rs");',
        ),
    )

    with pytest.raises(TrustAuditError, match="hidden"):
        items(source)


def test_an_opaque_macro_alias_is_followed_through_a_module() -> None:
    """Keep the opaque item's identity across a local module re-export."""
    source = "\n".join(
        (
            "mod m { pub use std::include as load; }",
            "use m::load as hidden;",
            'fn f() { hidden!("hidden.rs"); }',
        ),
    )

    with pytest.raises(TrustAuditError, match="hidden"):
        items(source)
