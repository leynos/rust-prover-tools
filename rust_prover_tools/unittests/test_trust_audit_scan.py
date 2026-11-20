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
    assert "assume" in [token.text for token in tokens]


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
