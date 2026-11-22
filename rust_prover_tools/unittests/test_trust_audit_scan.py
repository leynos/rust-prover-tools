"""Prove the trust scan reports only the vocabulary a context makes trust."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, scan
from rust_prover_tools.trust_audit.syntax import tokenize
from rust_prover_tools.unittests.trust_audit_scan_support import items


def test_a_qualified_kani_path_is_reported() -> None:
    """Report a Kani item reached through the crate's own name."""
    assert items("fn f() {\n    kani::assume(x);\n}") == [("assume", 2)]


def test_a_bare_word_outside_a_trust_import_is_not_reported() -> None:
    """Leave an ordinary identifier alone when the file imports no trust root."""
    assert items("fn assume(x: bool) { let external = x; }") == []


def test_a_kani_only_word_is_not_reported_from_a_generic_root() -> None:
    """Require the Kani root itself rather than any qualified path."""
    assert items("other::assume(x);") == []


def test_a_call_through_an_external_module_alias_is_reported() -> None:
    """Report a word behind a module alias whose re-export the file cannot see.

    ``exports`` may re-export Kani under ``k`` in another file, so the
    per-file scan cannot rule the call out.
    """
    assert items("fn f() { exports::k::assume(true); }") == [("assume", 1)]


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


def test_a_method_call_is_not_a_bare_use() -> None:
    """Leave a field or method of the same name alone."""
    source = "use kani;\nfn f(s: S) { s.assume(true); }"
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


def test_a_decomposed_identifier_matches_its_composed_spelling() -> None:
    """Compare identifiers by NFC, as Rust does.

    The alias is written with a combining accent and the call with the
    precomposed character; Rust treats the names as equal, so the call is the
    qualified Kani path the alias already binds.
    """
    source = "use kani as cafe\u0301;\nfn f() { caf\u00e9::assume(true); }"
    assert items(source) == [("assume", 2)]


def test_an_unterminated_literal_is_refused() -> None:
    """Refuse source whose literal never ends, rather than resuming mid-string."""
    with pytest.raises(TrustAuditError, match="unterminated string"):
        scan('fn f() { let _ = "open; }')


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

    words = [token.text for token in tokenize(source) if token.kind == "ident"]
    assert "allow" in words, "the inner attribute must survive tokenization"
    assert "dead_code" in words
    assert items(source) == [("assume", 2)]


def test_a_numeric_literal_suffix_is_not_an_identifier() -> None:
    """Keep a numeric literal's suffix inside the literal, as Rust lexes it."""
    words = [
        token.text for token in tokenize("ignore!(1assume);") if token.kind == "ident"
    ]

    assert "assume" not in words, "a numeric suffix must not become an identifier"
    assert items("ignore!(1assume);") == []


def test_a_leading_bom_does_not_hide_a_shebang() -> None:
    """Strip a leading BOM before deciding whether the first line is a shebang."""
    source = '﻿#!/usr/bin/env "run\nkani::assume(x);'

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


def test_a_character_literal_suffix_is_not_an_identifier() -> None:
    """Consume a character literal's suffix, as Rust does for its token."""
    words = [
        token.text
        for token in tokenize("let _ = 'x'suffix; let y = 1;")
        if token.kind == "ident"
    ]

    assert words == ["let", "_", "let", "y"]


def test_an_inner_attribute_survives_whitespace_after_the_bang() -> None:
    """Keep an inner attribute when trivia separates ``#!`` from ``[``."""
    source = "#! [allow(dead_code)]\nkani::assume(x);"

    words = [token.text for token in tokenize(source) if token.kind == "ident"]
    assert words == ["allow", "dead_code", "kani", "assume", "x"]
    assert items(source) == [("assume", 2)]
