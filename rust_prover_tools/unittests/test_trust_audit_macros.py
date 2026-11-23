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
    assert items(source) == [], f"include_str! must request no trust for {source!r}"


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

    assert items(source) == [], "a matcher import rename must bind no alias"


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

    assert not scan(source).items, (
        "a metavariable inside stringify! must not be reported"
    )


def test_an_opaque_macro_inside_stringify_is_not_refused() -> None:
    """``stringify!`` turns its argument into text; the macro inside does not run."""
    source = 'const S: &str = stringify!(include!("absent.rs"));'

    assert not scan(source).items, "concat! is trivia in an ordinary crate"


def test_a_metavariable_inside_stringify_is_not_refused() -> None:
    """A macro definition inside ``stringify!`` is text, not an expansion."""
    source = (
        "const S: &str = stringify! { "
        "macro_rules! m { ($p:ident) => { $p::assume(true); } } };"
    )

    assert not scan(source).items, "a definition inside a matcher must define nothing"


def test_a_locally_defined_stringify_keeps_the_opaque_refusal() -> None:
    """A local ``stringify`` macro compiles its argument, so nothing is exempt."""
    source = "\n".join(
        (
            "macro_rules! stringify { ($($t:tt)*) => { $($t)* }; }",
            'stringify!(include!("hidden.rs"));',
        ),
    )

    with pytest.raises(TrustAuditError, match="include!"):
        scan(source)


def test_an_opaque_macro_inside_stringify_is_refused_for_a_proc_macro() -> None:
    """A proc-macro crate can parse the generated text, so keep the refusal."""
    source = 'const S: &str = stringify!(include!("absent.rs"));'

    with pytest.raises(TrustAuditError, match="include!"):
        scan(source, procedural_macro=True)


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

    assert not scan(source).items, (
        "a metavariable invocation in a nested matcher must not run"
    )


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

    assert items(source) == [], "include_bytes! is trivia in an ordinary crate"


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

    assert items(source) == [], "a macro inside stringify! must not run"


def test_a_qualified_stringify_keeps_the_opaque_refusal() -> None:
    """A qualified ``stringify`` may name a user macro, so no exemption applies."""
    source = 'fn f() { other::stringify!(include!("hidden.rs")); }'

    with pytest.raises(TrustAuditError, match="include!"):
        scan(source)


def test_an_imported_stringify_keeps_the_opaque_refusal() -> None:
    """An imported ``stringify`` may name a user macro, so no exemption applies."""
    source = 'use other::stringify;\nfn f() { stringify!(include!("hidden.rs")); }'

    with pytest.raises(TrustAuditError, match="include!"):
        scan(source)


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
