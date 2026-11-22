"""Prove the token walk reports exactly the module files a source names."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError
from rust_prover_tools.unittests.trust_audit_tree_support import references


def test_an_inline_module_body_names_no_file() -> None:
    """Report ``mod x { … }`` as no module reference: its code is already here."""
    assert references("mod x { fn f() {} }") == []


def test_a_bare_module_declaration_names_a_file() -> None:
    """Report the name of ``mod x;`` for the caller to resolve."""
    assert references("mod x;") == [("x", None, ())]


def test_a_path_attribute_names_the_file_directly() -> None:
    """Carry the ``#[path]`` value rather than searching for the module."""
    assert references('#[path = "other.rs"]\nmod x;') == [("x", "other.rs", ())]


def test_a_path_attribute_is_found_past_a_visibility_modifier() -> None:
    """Step over ``pub(crate)``, which may sit between attribute and ``mod``."""
    source = '#[path = "other.rs"]\npub(crate) mod x;'
    assert references(source) == [("x", "other.rs", ())]


@pytest.mark.parametrize(
    "source",
    [
        '#[path = "other.rs"]\n#[allow(dead_code)]\nmod x;',
        '#[allow(dead_code)]\n#[path = "other.rs"]\nmod x;',
        '#[path = "other.rs"]\n#[cfg(test)]\nmod x;',
    ],
    ids=["path-first", "unrelated-first", "cfg-after-path"],
)
def test_a_path_attribute_is_found_past_an_unrelated_attribute(
    source: str,
) -> None:
    """Search the whole attribute run, not only the one nearest ``mod``."""
    assert references(source) == [("x", "other.rs", ())]


def test_a_nested_declaration_carries_its_inline_chain() -> None:
    """Report the inline modules a declaration sits inside, outermost first."""
    source = "mod outer { mod inner { mod x; } }"
    assert references(source) == [("x", None, ("outer", "inner"))]


def test_a_module_inside_stringify_is_not_a_declaration() -> None:
    """Skip a ``mod`` in ``stringify!``, which compiles none of its argument."""
    source = "stringify!(mod ghost;)\nmod real;"

    assert references(source) == [("real", None, ())]


def test_a_macro_pattern_is_not_a_module_declaration() -> None:
    """Ignore ``mod sample;`` written as the input a macro matches.

    The pattern is a template for source the macro is given, not source the
    crate compiles, so the declaration beside it is the only one reported.
    """
    source = "macro_rules! m { (mod sample;) => {} }\nmod real;"
    assert references(source) == [("real", None, ())]


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("(", ")"), ("[", "]"), ("{", "}")],
)
def test_a_macro_pattern_in_any_delimiter_is_not_a_declaration(
    opening: str,
    closing: str,
) -> None:
    """Skip the pattern however the ``macro_rules!`` body is bracketed."""
    source = f"macro_rules! m {opening}(mod sample;) => {{}}{closing}\nmod real;"
    assert references(source) == [("real", None, ())]


def test_a_module_named_by_a_macro_metavariable_is_refused() -> None:
    """Refuse ``mod $name;`` in an expansion the scan cannot expand.

    The invocation chooses the file, so the audit cannot tell which source the
    crate compiles.  Missing the declaration would report a clean scan of code
    it never read.
    """
    source = "macro_rules! decl { ($n:ident) => { mod $n; }; }"
    with pytest.raises(TrustAuditError, match="named by a metavariable"):
        references(source)


def test_a_literal_module_declaration_in_an_expansion_is_reported() -> None:
    """Keep a declaration an expansion spells, attributed to its invocation."""
    source = "macro_rules! decl { () => { mod proofs; }; }\ndecl!();"
    assert references(source) == [("proofs", None, ())]


def test_a_macro_alias_bound_to_two_definitions_is_refused() -> None:
    """Refuse one local name that two modules bind to different macros."""
    source = "\n".join(
        (
            "macro_rules! left { () => { mod one; }; }",
            "pub(crate) use left;",
            "macro_rules! right { () => { mod two; }; }",
            "pub(crate) use right;",
            "mod x { pub use crate::left as load; }",
            "mod y { pub use crate::right as load; }",
            "use x::load;",
            "use y::load;",
            "fn f() { load!(); }",
        ),
    )

    with pytest.raises(TrustAuditError, match="bound to both"):
        references(source)


def test_a_chained_macro_alias_compiles_its_module() -> None:
    """Resolve a rename of a rename before attributing the expansion."""
    source = "\n".join(
        (
            "macro_rules! make { () => { mod hidden; }; }",
            "pub(crate) use make;",
            "use crate::make as first;",
            "use first as second;",
            "second!();",
        ),
    )

    assert references(source) == [("hidden", None, ())]


def test_a_module_forwarded_through_a_macro_argument_is_refused() -> None:
    """Refuse a module the macro may place inside an inline module."""
    source = "\n".join(
        (
            "macro_rules! wrap { ($($t:tt)*) => { mod nested { $($t)* } }; }",
            "wrap! { mod child; }",
        ),
    )

    with pytest.raises(TrustAuditError, match="passes a module declaration"):
        references(source)


def test_a_forwarded_module_inside_an_expansion_is_refused() -> None:
    """Refuse a forwarded declaration written inside another macro's expansion."""
    source = "\n".join(
        (
            "macro_rules! wrap { ($($t:tt)*) => { mod nested { $($t)* } }; }",
            "macro_rules! relay { () => { wrap! { mod child; } }; }",
            "relay!();",
        ),
    )

    with pytest.raises(TrustAuditError, match="passes a module declaration"):
        references(source)


def test_a_forwarded_module_through_a_macro_alias_is_refused() -> None:
    """Resolve an invocation alias before checking what it forwards."""
    source = "\n".join(
        (
            "macro_rules! wrap { ($($t:tt)*) => { mod nested { $($t)* } }; }",
            "pub(crate) use wrap;",
            "use crate::wrap as build;",
            "build! { mod child; }",
        ),
    )

    with pytest.raises(TrustAuditError, match="passes a module declaration"):
        references(source)


def test_a_macro_invoked_through_an_alias_compiles_its_module() -> None:
    """Resolve the ``use`` alias before attributing an expansion's declaration.

    Rust expands ``build!()`` as the definition of ``make``, so the module it
    declares is compiled and must be reported.
    """
    source = "\n".join(
        (
            "macro_rules! make { () => { mod hidden; }; }",
            "pub(crate) use make;",
            "use crate::make as build;",
            "build!();",
        ),
    )

    assert references(source) == [("hidden", None, ())]


def test_the_first_of_two_path_attributes_is_the_file_compiled() -> None:
    """Follow the attribute ``rustc`` compiles when a declaration carries two.

    ``rustc`` takes the first ``#[path]`` in source order and warns that later
    ones are unused, so reading the attribute nearest the declaration would
    audit a file the crate does not compile.
    """
    source = '#[path = "first.rs"]\n#[path = "second.rs"]\nmod x;'
    assert references(source) == [("x", "first.rs", ())]


def test_a_unicode_module_name_names_its_path_file() -> None:
    """Keep the whole name so a ``#[path]`` module is not silently skipped."""
    source = '#[path = "coffee.rs"]\nmod café;'
    assert references(source) == [("café", "coffee.rs", ())]


@pytest.mark.parametrize("spelling", [r"\x63", r"\u{63}", r"\u{6_3}"])
def test_a_path_escape_is_read_as_rust_reads_it(spelling: str) -> None:
    r"""Decode every spelling of ``c`` rather than reading the escape itself.

    Rust accepts the cooked ``\x63``, the braced ``\u{63}`` and the
    underscore-separated ``\u{6_3}``, and all three name the same file.
    """
    source = f'#[path = "{spelling}hild.rs"]\nmod child;'
    assert references(source) == [("child", "child.rs", ())]


@pytest.mark.parametrize(
    ("spelling", "pattern"),
    [
        (r"\u{0000063}", "malformed"),
        (r"\u{d800}", "not a Unicode scalar value"),
        (r"\u{_63}", "malformed"),
    ],
)
def test_an_undecodable_path_escape_is_refused(spelling: str, pattern: str) -> None:
    r"""Refuse a ``\u{…}`` escape Rust rejects rather than guessing a path.

    Seven digits exceed Rust's six-digit limit, and a surrogate is not a code
    point Rust will compile.
    """
    source = f'#[path = "{spelling}.rs"]\nmod x;'
    with pytest.raises(TrustAuditError, match=pattern):
        references(source)


def test_a_metavariable_path_attribute_is_refused() -> None:
    """Refuse a ``#[path]`` whose value only the expansion's arguments decide."""
    source = "macro_rules! load { ($p:literal) => { #[path = $p] mod child; }; }"

    with pytest.raises(TrustAuditError, match="cannot resolve"):
        references(source)


def test_a_cfg_attr_with_a_non_literal_path_is_refused() -> None:
    """Refuse a conditional path whose value is an expression, not a literal."""
    source = '#[cfg_attr(feature = "alt", path = concat!("alt", ".rs"))]\nmod platform;'

    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_conditional_path_is_refused() -> None:
    """Refuse a declaration whose file a configuration decides."""
    source = '#[cfg_attr(unix, path = "unix_impl.rs")]\nmod platform;'
    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_conditional_path_above_a_direct_path_is_refused() -> None:
    """Examine the whole attribute run, whichever order the two appear in."""
    source = (
        '#[cfg_attr(feature = "alt", path = "alt.rs")]\n#[path = "base.rs"]\nmod child;'
    )
    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_cfg_attr_predicate_naming_path_is_not_a_path_attribute() -> None:
    """Read ``path`` in the predicate as a configuration name, not a target."""
    source = '#[cfg_attr(path = "audit", allow(dead_code))]\nmod x;'
    assert references(source) == [("x", None, ())]


def test_a_declaration_after_an_inline_module_is_at_file_scope() -> None:
    """Leave the chain empty once the inline module's brace has closed."""
    source = "mod outer { mod x; }\nmod y;"
    assert references(source) == [("x", None, ("outer",)), ("y", None, ())]


def test_a_nested_cfg_attr_predicate_is_not_a_path_attribute() -> None:
    """Read a nested ``cfg_attr`` predicate as configuration, not as ``path``.

    ``cfg_attr(path = "fast", allow(dead_code))`` configures by a custom key
    named ``path`` and applies only ``allow``, so the declaration still uses
    the default module file and must not be refused.
    """
    source = '#[cfg_attr(unix, cfg_attr(path = "fast", allow(dead_code)))]\nmod child;'

    assert references(source) == [("child", None, ())]


def test_a_nested_cfg_attr_payload_can_still_select_a_path() -> None:
    """Refuse a nested payload that applies a ``path`` attribute."""
    source = (
        '#[cfg_attr(unix, cfg_attr(feature = "fast", path = "alt.rs"))]\nmod child;'
    )

    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_an_array_bracket_before_a_path_attribute_keeps_the_value() -> None:
    """Stop the attribute walk at non-attribute code, keeping what it found."""
    source = 'type Block = [u8; 4];\n#[path = "selected.rs"]\nmod child;'

    assert references(source) == [("child", "selected.rs", ())]
