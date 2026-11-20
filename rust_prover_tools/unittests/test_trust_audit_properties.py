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


@given(WORD, st.integers(min_value=0, max_value=12))
def test_a_word_inside_a_raw_string_is_not_reported(word: str, hashes: int) -> None:
    """Close a raw string on the fence it opened with, not a shorter one."""
    fence = "#" * hashes
    source = f'fn f() {{ let _ = r{fence}"kani::{word}("{fence}; }}'
    assert not _items(source)


@given(WORD)
def test_a_word_inside_a_character_literal_is_not_reported(word: str) -> None:
    """Read a character literal whole, so its closing quote reopens nothing."""
    first = word[0]
    assert _items(f"fn f() {{ let _ = '{first}'; kani::assume(x); }}") == ("assume",)


@given(st.lists(BODY_FRAGMENT, max_size=8).map("".join))
def test_an_ordinary_string_body_yields_no_item(body: str) -> None:
    """Scan a string body as trivia even when it spells the whole vocabulary.

    The body is built from vocabulary words, complete escapes and plain text,
    so a word can sit directly after an escaped quote — the position where a
    lexer that mistook the escape for the terminator would resume mid-literal.
    """
    source = f'fn f() {{ let _ = "{body}"; }}'
    assert not _items(source)


@given(KANI_WORD, KANI_WORD)
def test_each_qualified_path_is_reported_in_source_order(
    first: str,
    second: str,
) -> None:
    """Report each occurrence separately, even when the two words are equal."""
    source = f"kani::{first}(x);\nkani::{second}(y);"
    assert _items(source) == (first, second)
