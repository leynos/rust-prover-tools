"""Property tests for the trust audit's fail-closed direction.

The audit is only useful if it never reports a word the *lexer* invented. A
word inside a comment or a literal is trivia, and the failure this guards
against is the lexer stopping short of a terminator, resuming mid-literal, and
reading the literal's remaining text as code. The vocabulary is the same set
the matcher reports, so any such leak becomes an item.
"""

from __future__ import annotations

import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from rust_prover_tools.trust_audit import scan
from rust_prover_tools.trust_audit.vocabulary import KANI_VOCABULARY, VOCABULARY

#: One word either prover reports when it lands in the right context.
WORD: typ.Final[st.SearchStrategy[str]] = st.sampled_from(sorted(VOCABULARY))
#: One word the ``kani::`` path rule reports, which the Verus-only words are not.
KANI_WORD: typ.Final[st.SearchStrategy[str]] = st.sampled_from(
    sorted(KANI_VOCABULARY),
)
#: Complete escapes a cooked literal may carry.  Each ends where it began, so
#: joining them never leaves a dangling backslash that would escape the closing
#: quote and make the failure a refusal rather than a leak.
COMPLETE_ESCAPE: typ.Final[st.SearchStrategy[str]] = st.sampled_from(
    ("\\\\", '\\"', "\\n", "\\r", "\\t", "\\0", "\\x41", "\\u{63}"),
)
#: One fragment of a literal body: a vocabulary word, a complete escape, or
#: plain text.  A word may therefore follow an escaped quote, which is the
#: position where a lexer that stopped short of the escape would resume.
BODY_FRAGMENT: typ.Final[st.SearchStrategy[str]] = st.one_of(
    st.sampled_from(sorted(VOCABULARY)),
    COMPLETE_ESCAPE,
    st.text(alphabet=st.characters(blacklist_characters='"\\'), max_size=12),
)


def _items(source: str) -> tuple[str, ...]:
    """Return the words the scan reports for ``source``."""
    return tuple(item.item for item in scan(source).items)


@given(WORD)
def test_a_word_inside_a_line_comment_is_not_reported(word: str) -> None:
    """Read a ``//`` comment as trivia, whatever it contains."""
    assert not _items(f"// kani::{word}(x)\nfn f() {{}}")


@given(WORD)
def test_a_word_inside_a_block_comment_is_not_reported(word: str) -> None:
    """Read a ``/* */`` comment as trivia, whatever it contains."""
    assert not _items(f"/* kani::{word}(x) */ fn f() {{}}")


@given(WORD)
def test_a_word_inside_a_plain_string_is_not_reported(word: str) -> None:
    """Read an ordinary string body as trivia."""
    source = f'fn f() {{ let _ = "kani::{word}(x)"; }}'
    assert not _items(source)


@given(WORD, st.integers(min_value=1, max_value=12))
def test_a_word_after_a_shorter_raw_fence_is_not_reported(
    word: str,
    hashes: int,
) -> None:
    """Keep a raw string open across a quote with too few hashes.

    The body holds a quote followed by one hash fewer than the fence, so a
    lexer that stopped at the first quote would end the literal early and
    expose ``kani::{word}`` as code.  A real call after the string is the
    control: the scan reports it and nothing from inside.
    """
    fence = "#" * hashes
    shorter = '"' + "#" * (hashes - 1)
    body = f"{shorter} kani::{word}({shorter}"
    source = f'fn f() {{ let _ = r{fence}"{body}"{fence}; kani::assume(x); }}'
    assert _items(source) == ("assume",)


@given(WORD)
def test_a_word_inside_a_character_literal_is_not_reported(word: str) -> None:
    """Read a character literal whole, so its closing quote reopens nothing."""
    first = word[0]
    assert _items(f"fn f() {{ let _ = '{first}'; kani::assume(x); }}") == ("assume",)


@given(WORD)
def test_a_bare_word_after_a_trust_import_is_reported(word: str) -> None:
    """Establish the control the string-body property is measured against.

    ``use kani;`` switches the bare-word rule on, so a word the lexer wrongly
    exposes as code becomes an item.  Without this control a scan that reported
    nothing at all would pass the string-body property.
    """
    assert _items(f"use kani;\nfn f() {{ let _ = {word}; }}") == (word,)


@given(st.lists(BODY_FRAGMENT, max_size=8).map("".join))
def test_an_ordinary_string_body_yields_no_item(body: str) -> None:
    """Scan a string body as trivia even when it spells the whole vocabulary.

    The body is built from vocabulary words, complete escapes and plain text,
    so a word can sit directly after an escaped quote — the position where a
    lexer that mistook the escape for the terminator would resume mid-literal.
    The surrounding file imports ``kani``, so a word the lexer leaks out of the
    literal is reportable, and the empty result means nothing leaked.
    """
    source = f'use kani;\nfn f() {{ let _ = "{body}"; }}'
    assert not _items(source)


@given(KANI_WORD, KANI_WORD)
def test_each_qualified_path_is_reported_in_source_order(
    first: str,
    second: str,
) -> None:
    """Report each occurrence separately, even when the two words are equal."""
    source = f"kani::{first}(x);\nkani::{second}(y);"
    assert _items(source) == (first, second)
