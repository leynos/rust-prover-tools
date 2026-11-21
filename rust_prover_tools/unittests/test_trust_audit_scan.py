"""Prove the trust scan reports only the vocabulary a context makes trust."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, scan
from rust_prover_tools.trust_audit.syntax import tokenize


def items(source: str, *, procedural_macro: bool = False) -> list[tuple[str, int]]:
    """Return each reported item with the line it was found on."""
    return [
        (item.item, item.line)
        for item in scan(source, procedural_macro=procedural_macro).items
    ]


def test_a_qualified_kani_path_is_reported() -> None:
    """Report a Kani item reached through the crate's own name."""
    assert items("fn f() {\n    kani::assume(x);\n}") == [("assume", 2)]


def test_a_bare_word_outside_a_trust_import_is_not_reported() -> None:
    """Leave an ordinary identifier alone when the file imports no trust root."""
    assert items("fn assume(x: bool) { let external = x; }") == []


def test_a_kani_only_word_is_not_reported_from_a_generic_root() -> None:
    """Require the Kani root itself rather than any qualified path."""
    assert items("other::assume(x);") == []


def test_a_named_kani_import_makes_a_bare_word_trust() -> None:
    """Apply the imported-root rule to a bare use after a named import."""
    source = "use kani::assume;\nfn f() { assume(true); }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_kani_crate_alias_makes_a_qualified_path_trust() -> None:
    """Match a qualified path through the name an alias binds."""
    source = "use kani as k;\nfn f() { k::assume(true); }"
    assert items(source) == [("assume", 2)]


def test_a_grouped_import_of_a_trust_root_enables_the_bare_rule() -> None:
    """Count every root of a grouped import, not only its first."""
    source = "use {std::mem, kani};\nfn f() { assume(true); }"
    assert items(source) == [("assume", 2)]


def test_a_member_rename_does_not_bind_the_crate() -> None:
    """Treat ``use kani::{assume as a};`` as an item rename, not a crate one."""
    source = "use kani::{assume as a};\nfn f() { a::assume(true); }"
    assert items(source) == [("assume", 1)]


def test_a_member_rename_reports_the_bare_call_under_the_item_name() -> None:
    """Report ``a(true)`` as the Kani item the import renamed."""
    source = "use kani::{assume as a};\nfn f() { a(true); }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_self_qualified_member_rename_is_reported() -> None:
    """Read ``self::a(..)`` as the item the import bound to ``a``.

    A named import binds its item in the module it appears in, so the ``self``
    qualifier names the same call the bare ``a(..)`` names.
    """
    source = "use kani::assume as a;\nfn f() { self::a(false); }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_crate_qualified_member_rename_is_reported() -> None:
    """Read ``crate::a(..)`` as the binding a crate-root import made.

    A ``use`` at the crate root binds its item there, so a child module reaches
    the same call through ``crate::``.  The scan is file-scoped and cannot see
    which module holds the import, so the qualified call is reported rather
    than dropped.
    """
    source = "use kani::assume as a;\nfn f() { crate::a(false); }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_super_qualified_member_rename_is_reported() -> None:
    """Read ``super::a(..)`` as a binding the parent module imported."""
    source = "use kani::assume as a;\nmod child { fn f() { super::a(false); } }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_foreign_qualified_member_rename_is_not_reported() -> None:
    """Leave another module's qualifier alone: its binding is not shown."""
    source = "use kani::assume as a;\nfn f() { other::a(false); }"
    assert items(source) == [("assume", 1)]


def test_two_scopes_binding_one_name_report_both_items() -> None:
    """Report every item a local name may denote when two scopes reuse it.

    The scan is file-scoped, so it cannot tell which module a later use sits
    in.  Keeping only one binding would hide the other item entirely.
    """
    source = (
        "mod a { use kani::assume as m; }\n"
        "mod b { use kani::any_where as m; }\n"
        "fn f() { m(true); }"
    )
    assert items(source) == [
        ("assume", 1),
        ("any_where", 2),
        ("any_where", 3),
        ("assume", 3),
    ]


def test_a_chained_crate_alias_makes_a_qualified_path_trust() -> None:
    """Follow a rename of a rename to the crate both names reach."""
    source = "use kani as k;\nuse k as kk;\nfn f() { kk::assume(true); }"
    assert items(source) == [("assume", 3)]


def test_a_member_rename_through_a_crate_alias_is_reported() -> None:
    """Resolve ``use k::{assume as a}`` once ``use kani as k`` binds the crate."""
    source = "use kani as k;\nuse k::{assume as a};\nfn f() { a(true); }"
    assert items(source) == [("assume", 2), ("assume", 3)]


def test_a_chained_member_rename_reports_the_original_item() -> None:
    """Follow ``use a as b`` from a member rename to the item it renames."""
    source = "use kani::{assume as a};\nuse a as b;\nfn f() { b(true); }"
    assert items(source) == [("assume", 1), ("assume", 2), ("assume", 3)]


def test_a_self_qualified_member_rename_is_followed() -> None:
    """Follow ``use self::a as b`` to the item the earlier import bound to ``a``."""
    source = "use kani::assume as a;\nuse self::a as b;\nfn f() { b(true); }"
    assert items(source) == [("assume", 1), ("assume", 2), ("assume", 3)]


def test_a_crate_qualified_member_rename_is_followed() -> None:
    """Follow ``use crate::a as b`` to the item bound at the crate root."""
    source = "use kani::assume as a;\nuse crate::a as b;\nfn f() { b(true); }"
    assert items(source) == [("assume", 1), ("assume", 2), ("assume", 3)]


def test_a_super_qualified_member_rename_is_followed() -> None:
    """Follow ``use super::a as b`` to the item the parent module bound."""
    source = (
        "use kani::assume as a;\nmod child { use super::a as b; fn f() { b(true); } }"
    )
    assert items(source) == [("assume", 1), ("assume", 2), ("assume", 2)]


def test_a_member_rename_before_a_path_is_not_a_call() -> None:
    """Leave the qualifier of ``a::assume(..)`` alone: it names no item."""
    source = "use kani::{assume as a};\nfn f() { a::assume(true); }"
    assert [item for item in items(source) if item[1] == 2] == []


def test_a_method_call_is_not_a_bare_use() -> None:
    """Leave a field or method of the same name alone."""
    source = "use kani;\nfn f(s: S) { s.assume(true); }"
    assert items(source) == []


@pytest.mark.parametrize(
    ("opener", "closer"),
    [("{", "}"), ("(", ")"), ("[", "]")],
)
def test_a_verus_body_reports_its_verus_vocabulary(opener: str, closer: str) -> None:
    """Read a ``verus!`` body under any delimiter pair the macro accepts."""
    source = f"verus!{opener}proof fn p() {{ admit(); }}{closer};"
    assert items(source) == [("admit", 1)]


def test_a_verus_body_does_not_report_a_kani_only_word() -> None:
    """Use each prover's own vocabulary for its own context."""
    source = "verus! { fn f() { let stub = 1; } }"
    assert items(source) == []


def test_a_renamed_kani_member_inside_a_verus_body_is_reported() -> None:
    """Apply the imported-root rule inside a ``verus!`` body too.

    The rules are cumulative: a Kani-only word in a Verus body is not Verus
    vocabulary, but it is still the item a Kani import renamed.
    """
    source = "use kani::{assume as a};\nverus! { proof fn f() { a(true); } }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word inside a bare ``external`` attribute."""
    source = "#[verifier(external_body)]\nfn f() {}"
    assert items(source) == [("external_body", 1)]


def test_an_assume_termination_attribute_is_reported() -> None:
    """Report Verus' termination assumption as the request for trust it is.

    The reference calls the attribute currently unneeded, but it still tells
    the verifier to accept a termination argument without proof, so the
    inventory must name it.
    """
    source = "#[verifier::assume_termination]\nfn f() {}"
    assert items(source) == [("assume_termination", 1)]


def test_a_cfg_attr_carrying_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word nested inside a ``cfg_attr``."""
    source = "#[cfg_attr(verus_keep_ghost, verus_spec(admit()))]\nfn f() {}"
    assert items(source) == [("admit", 1)]


def test_a_cfg_attr_carrying_a_qualified_attribute_reports_it() -> None:
    """Keep every segment of ``verifier::external_body`` inside the region."""
    source = "#[cfg_attr(enabled, verifier::external_body)]\nfn f() {}"
    assert items(source) == [("external_body", 1)]


def test_a_cfg_attr_without_a_verus_attribute_reports_nothing() -> None:
    """Leave a ``cfg_attr`` that carries no Verus attribute alone."""
    source = "#[cfg_attr(test, should_panic)]\nfn f() {}"
    assert items(source) == []


def test_a_cfg_attr_predicate_is_not_read_as_a_verus_attribute() -> None:
    """Read only the payload: ``verifier`` in the predicate is a condition."""
    source = "#[cfg_attr(all(verifier, external), inline)]\nfn f() {}"
    assert items(source) == []


def test_commented_and_quoted_vocabulary_is_not_code() -> None:
    """Read neither a comment nor a string as a path."""
    source = (
        "// kani::assume(x)\n/* kani::assume(y) */\n"
        'fn f() { let _ = "kani::assume(z)"; }'
    )
    assert items(source) == []


def test_a_raw_identifier_ending_a_qualified_path_is_reported() -> None:
    """Strip the raw prefix so ``kani::r#assume`` is the same call."""
    assert items("kani::r#assume(x);") == [("assume", 1)]


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


def test_a_decomposed_identifier_matches_its_composed_spelling() -> None:
    """Compare identifiers by NFC, as Rust does.

    The alias is written with a combining accent and the call with the
    precomposed character; Rust treats the names as equal, so the call is the
    qualified Kani path the alias already binds.
    """
    source = "use kani as cafe\u0301;\nfn f() { caf\u00e9::assume(true); }"
    assert items(source) == [("assume", 2)]


@pytest.mark.parametrize("separator", ["\f", "\v", "\u200e"])
def test_rust_whitespace_separates_a_verus_macro_head(separator: str) -> None:
    """Treat every Rust whitespace character as a token separator."""
    source = f"verus{separator}!{{ proof fn p() {{ admit(); }} }}"
    assert items(source) == [("admit", 1)]


def test_an_unterminated_literal_is_refused() -> None:
    """Refuse source whose literal never ends, rather than resuming mid-string."""
    with pytest.raises(TrustAuditError, match="unterminated string"):
        scan('fn f() { let _ = "open; }')


def test_a_procedural_macro_scan_reads_literals() -> None:
    """Report the vocabulary a proc-macro crate could expand into code."""
    source = 'fn f() { let _ = "kani::assume(x); assume(y);"; }'
    assert items(source, procedural_macro=True) == [("assume", 1), ("assume", 1)]


def test_a_literal_is_not_read_without_the_procedural_macro_toggle() -> None:
    """Keep a string's contents out of the audit for an ordinary crate."""
    source = 'fn f() { let _ = "kani::assume(x);"; }'
    assert items(source) == []


def test_a_procedural_macro_scan_decodes_a_literal_escape() -> None:
    r"""Report what Rust decodes ``\x61`` to, at the escape that wrote it."""
    source = r'"kani::\x61ssume(x);"'
    found = scan(source, procedural_macro=True).items
    assert [(item.item, item.column) for item in found] == [("assume", 8)]


def test_a_procedural_macro_scan_refuses_an_undecodable_escape() -> None:
    """Fail closed rather than report a clean literal it could not read."""
    with pytest.raises(TrustAuditError, match="not a Unicode scalar value"):
        scan(r'"\u{d800}assume"', procedural_macro=True)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_line_continuation_is_not_part_of_the_literal_value(newline: str) -> None:
    r"""Skip the newline and indentation a backslash continues past.

    Rust normalizes a CRLF to LF before reading the literal, so the two
    spellings hide the newline alike.
    """
    source = f'"kani::assume\\{newline}    (x);"'
    found = scan(source, procedural_macro=True).items
    assert [(item.item, item.column) for item in found] == [("assume", 8)]


def test_a_literal_scan_keeps_two_occurrences_apart() -> None:
    """Locate each literal match so two in one token stay distinguishable."""
    source = '"admit(); admit();"'
    columns = [item.column for item in scan(source, procedural_macro=True).items]
    assert columns == [2, 11]


def test_a_literal_scan_reports_in_source_order() -> None:
    """Report a bare match before a qualified one that follows it."""
    source = '"assume(x); kani::assume(y);"'
    columns = [item.column for item in scan(source, procedural_macro=True).items]
    assert columns == [2, 19]


def test_an_unterminated_character_literal_is_refused() -> None:
    """Refuse a quote that can start neither a character nor a lifetime."""
    with pytest.raises(TrustAuditError, match="unterminated character literal"):
        scan("fn f() { let c = '💡; }")


def test_a_unicode_module_name_is_one_identifier() -> None:
    """Keep a non-ASCII name whole, as Rust does."""
    names = [token.text for token in tokenize("mod café;") if token.kind == "ident"]
    assert names == ["mod", "café"]


def test_an_escaped_quote_ends_the_literal_at_the_right_place() -> None:
    """Continue a string past an escaped quote instead of splitting it."""
    tokens = tokenize(r'let _ = "a\"assume"; let x = 1;')
    assert [token.text for token in tokens if token.kind == "ident"] == [
        "let",
        "_",
        "let",
        "x",
    ]


def test_a_lifetime_is_not_a_character_literal() -> None:
    """Read ``'a`` as a lifetime so the code after it stays code."""
    tokens = tokenize("fn f<'a>(x: &'a u8) { kani::assume(true); }")
    words = [token.text for token in tokens]
    assert "x" in words, "the parameter between the lifetimes must survive"
    assert "assume" in words


def test_a_character_literal_holding_a_quote_does_not_reopen() -> None:
    """Close a character literal at its own quote, escaped or not."""
    tokens = tokenize("let q = '\"'; let r = '\\''; let s = 1;")
    assert [token.text for token in tokens if token.kind == "ident"] == [
        "let",
        "q",
        "let",
        "r",
        "let",
        "s",
    ]


def test_a_deeply_nested_use_statement_is_refused_as_a_trust_error() -> None:
    """Report a use tree past the recursion limit as an audit failure.

    The audit's callers turn a ``TrustAuditError`` into ``AUDIT_FAILED``; a
    ``RecursionError`` escaping as a traceback would abort the report they
    promise instead.
    """
    nesting = 5000
    source = f"use {'{' * nesting}kani{'}' * nesting};"
    with pytest.raises(TrustAuditError, match="nest too deeply"):
        scan(source)


def test_tokenize_refuses_a_nested_comment_that_never_closes() -> None:
    """Honour comment nesting when deciding a comment has ended."""
    with pytest.raises(TrustAuditError, match="unterminated block comment"):
        tokenize("/* a /* b */ kani::assume(x);")


def test_an_audit_may_select_one_provers_vocabulary() -> None:
    """Apply only the vocabulary a caller selected.

    A file can import both provers.  Selecting one is how a caller asks for
    that prover's surfaces alone, and the other prover's qualified path must
    not be reported by a rule the selection left out.
    """
    source = "\n".join(
        (
            "use vstd::prelude::*;",
            "fn f() { admit(); kani::assume(true); }",
        ),
    )

    from rust_prover_tools.trust_audit.vocabulary import KANI, VERUS

    kani_only = [item.item for item in scan(source, vocabularies=(KANI,)).items]
    verus_only = [item.item for item in scan(source, vocabularies=(VERUS,)).items]
    both = [item.item for item in scan(source).items]

    assert kani_only == ["assume"]
    assert verus_only == ["admit"]
    assert both == ["admit", "assume"]


def test_a_selected_verus_vocabulary_keeps_its_region_rule() -> None:
    """A ``verus!`` body gives a selected Verus word meaning without an import."""
    from rust_prover_tools.trust_audit.vocabulary import VERUS

    source = "verus! { fn f() { admit(); } }"

    assert [item.item for item in scan(source, vocabularies=(VERUS,)).items] == [
        "admit",
    ]


def test_a_selected_kani_vocabulary_does_not_read_a_verus_region() -> None:
    """A Kani audit leaves a Verus word in a ``verus!`` body alone.

    The region rule belongs to the vocabulary record, so selecting Kani removes
    it along with the Verus words it names.
    """
    from rust_prover_tools.trust_audit.vocabulary import KANI

    source = "verus! { fn f() { admit(); } }"

    assert scan(source, vocabularies=(KANI,)).items == ()


def test_an_xid_continue_character_stays_inside_an_identifier() -> None:
    """Keep a Unicode continuation such as the middle dot in the name.

    Rust identifiers are XID, which admits U+00B7 after the first character.
    Splitting there would turn ``mod a·b;`` into a punctuator the module walk
    cannot read, losing the file the declaration compiles.
    """
    tokens = tokenize("mod a·b;")

    assert [token.text for token in tokens if token.kind == "ident"] == [
        "mod",
        "a·b",
    ]


def test_a_shebang_is_not_read_as_code() -> None:
    """Skip a leading shebang even when its arguments hold a quote.

    Rust discards the line, so the unmatched quote must not open a string that
    swallows the code beneath it.
    """
    source = '#!/usr/bin/env "run-cargo-script\nkani::assume(x);'

    assert items(source) == [("assume", 2)]


def test_a_leading_inner_attribute_is_not_a_shebang() -> None:
    """Keep an inner attribute that begins with ``#!`` in the token stream."""
    source = "#![allow(dead_code)]\nkani::assume(x);"

    assert items(source) == [("assume", 2)]


def test_a_string_literal_suffix_is_not_an_identifier() -> None:
    """Consume a suffix as part of its literal, as Rust lexes it.

    ``"text"include`` is one literal token in a token tree.  Exposing
    ``include`` would refuse the file as an opaque ``include!`` invocation
    whose expansion the audit never sees.
    """
    assert items('ignore!("text"include!());') == []
    with pytest.raises(TrustAuditError, match=r"include! .* generates code"):
        scan('include!("text");')


def test_a_declared_module_qualifies_a_renamed_member() -> None:
    """Report a renamed import reached through a module the file declares.

    ``mod m { pub use kani::assume as a; }`` puts the binding in this file, so
    ``m::a(..)`` is the same call as the bare ``a(..)``.
    """
    source = "mod m { pub use kani::assume as a; }\nfn f() { m::a(true); }"

    assert items(source) == [("assume", 1), ("assume", 2)]


def test_an_undeclared_module_does_not_qualify_a_renamed_member() -> None:
    """Leave a path through a module this file never declares alone."""
    source = "use kani::assume as a;\nfn f() { elsewhere::a(true); }"

    assert items(source) == [("assume", 1)]


def test_a_plain_member_import_seeds_a_requalified_rename() -> None:
    """Follow ``self::`` to a member imported without a rename.

    ``use kani::assume; use self::assume as a;`` binds ``a`` to Kani's
    ``assume``, so a later ``a(..)`` is trust.
    """
    source = "use kani::assume;\nuse self::assume as a;\nfn f() { a(true); }"

    assert [item for item, _ in items(source)] == ["assume", "assume", "assume"]


def test_a_crate_alias_requalified_through_self_is_followed() -> None:
    """Follow a crate alias renamed again through a local qualifier.

    ``use kani as k; use self::k as kk;`` makes ``kk::assume`` the Kani call.
    """
    source = "use kani as k;\nuse self::k as kk;\nfn f() { kk::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


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


def test_a_grouped_plain_import_seeds_every_member() -> None:
    """Seed each unrenamed leaf of a grouped import, not only the last.

    ``use kani::{assume, any};`` binds both names, so a later
    ``use self::assume as a;`` reaches Kani's ``assume``.
    """
    source = "\n".join(
        (
            "use kani::{assume, any_where};",
            "use self::assume as a;",
            "fn f() { a(true); }",
        ),
    )

    assert items(source) == [
        ("assume", 1),
        ("any_where", 1),
        ("assume", 2),
        ("assume", 3),
    ]


def test_a_crate_alias_qualified_through_self_is_followed() -> None:
    """Resolve the crate alias in ``self::k::assume`` before the member.

    ``use kani as k; use self::k::assume as a;`` binds ``a`` to Kani's
    ``assume`` just as ``use k::assume as a;`` would.
    """
    source = "use kani as k;\nuse self::k::assume as a;\nfn f() { a(true); }"

    assert items(source) == [("assume", 2), ("assume", 3)]


def test_an_aliased_verus_macro_opens_a_region() -> None:
    """Treat a local name bound to ``verus`` as the same macro."""
    source = "use vstd::prelude::{verus as v, *};\nv! { fn f() { admit(); } }"

    assert items(source) == [("admit", 2)]


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


def test_an_inner_attribute_survives_whitespace_after_the_bang() -> None:
    """Keep an inner attribute when trivia separates ``#!`` from ``[``."""
    source = "#! [allow(dead_code)]\nkani::assume(x);"

    words = [token.text for token in tokenize(source) if token.kind == "ident"]
    assert words == ["allow", "dead_code", "kani", "assume", "x"]
    assert items(source) == [("assume", 2)]


def test_a_turbofish_call_to_a_renamed_member_is_reported() -> None:
    """Treat ``::<`` as explicit generic arguments, not a path separator.

    ``a::<u32, _>(..)`` calls the imported item, so its position is a trust
    use even though the item is renamed.
    """
    source = "use kani::any_where as a;\nfn f() { a::<u32, _>(|_| true); }"

    assert items(source) == [("any_where", 1), ("any_where", 2)]


def test_a_crate_alias_through_a_declared_module_is_followed() -> None:
    """Follow a crate alias re-exported by a module this file declares."""
    source = "\n".join(
        (
            "mod m { pub use kani as k; }",
            "use m::k as kk;",
            "fn f() { kk::assume(true); }",
        ),
    )

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_metavariable_path_to_a_trust_item_is_refused() -> None:
    """Refuse an expansion whose path qualifier only its arguments decide."""
    source = "macro_rules! call { ($p:path) => { $p::assume(true); }; }"

    with pytest.raises(TrustAuditError, match="reaches a trust item"):
        items(source)


def test_a_renamed_import_does_not_seed_its_original_name() -> None:
    """Bind only the new name, as Rust does.

    ``use kani::assume as a;`` does not also bind ``assume``, so a later
    ``use self::assume as b;`` must not be read as a rename of Kani's item.
    """
    source = "\n".join(
        (
            "use kani::assume as a;",
            "use self::assume as b;",
            "fn f() { b(true); }",
        ),
    )

    assert items(source) == [("assume", 1)]


def test_a_crate_alias_through_repeated_super_segments_is_followed() -> None:
    """Skip every leading ``super`` before the crate alias."""
    source = "\n".join(
        (
            "use kani as k;",
            "mod a {",
            "    mod b {",
            "        use super::super::k::assume as c;",
            "        fn f() { c(true); }",
            "    }",
            "}",
        ),
    )

    assert items(source) == [("assume", 4), ("assume", 5)]


def test_a_crate_root_import_is_treated_as_a_possible_alias() -> None:
    """Fail closed on a name imported from the crate root.

    The declaring file is outside this per-file scan, so the local name is
    reported as a possible trust alias rather than assumed harmless.
    """
    source = "use crate::k;\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_attribute_value_is_not_read_as_a_verus_attribute() -> None:
    """Skip an unrelated attribute's arguments in a ``cfg_attr`` payload.

    ``doc = stringify!(verifier::external_body)`` is documentation text, not
    an applied Verus attribute.
    """
    source = (
        '#[cfg_attr(feature = "docs", doc = stringify!(verifier::external_body))]\n'
        "fn f() {}"
    )

    assert items(source) == []


def test_a_nested_cfg_attr_payload_is_still_read() -> None:
    """Recurse into a nested ``cfg_attr`` that applies a Verus attribute."""
    source = (
        '#[cfg_attr(unix, cfg_attr(feature = "x", verifier::external_body))]\nfn f() {}'
    )

    assert [item for item, _ in items(source)] == ["external_body"]
