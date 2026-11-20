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


def test_a_method_call_is_not_a_bare_use() -> None:
    """Leave a field or method of the same name alone."""
    source = "use kani;\nfn f(s: S) { s.assume(true); }"
    assert items(source) == []


def test_a_verus_body_reports_its_verus_vocabulary() -> None:
    """Report a Verus word inside a ``verus!`` block."""
    source = "verus! {\n    proof fn p() { admit(); }\n}"
    assert items(source) == [("admit", 2)]


def test_a_verus_body_does_not_report_a_kani_only_word() -> None:
    """Use each prover's own vocabulary for its own context."""
    source = "verus! { fn f() { let stub = 1; } }"
    assert items(source) == []


def test_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word inside a bare ``external`` attribute."""
    source = "#[verifier(external_body)]\nfn f() {}"
    assert items(source) == [("external_body", 1)]


def test_a_cfg_attr_carrying_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word nested inside a ``cfg_attr``."""
    source = "#[cfg_attr(verus_keep_ghost, verus_spec(admit()))]\nfn f() {}"
    assert items(source) == [("admit", 1)]


def test_a_cfg_attr_without_a_verus_attribute_reports_nothing() -> None:
    """Leave a ``cfg_attr`` that carries no Verus attribute alone."""
    source = "#[cfg_attr(test, should_panic)]\nfn f() {}"
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


def test_a_literal_scan_keeps_two_occurrences_apart() -> None:
    """Locate each literal match so two in one token stay distinguishable."""
    source = '"admit(); admit();"'
    columns = [item.column for item in scan(source, procedural_macro=True).items]
    assert columns == [2, 11]


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


def test_tokenize_refuses_a_nested_comment_that_never_closes() -> None:
    """Honour comment nesting when deciding a comment has ended."""
    with pytest.raises(TrustAuditError, match="unterminated block comment"):
        tokenize("/* a /* b */ kani::assume(x);")
