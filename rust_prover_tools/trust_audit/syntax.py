"""Lex Rust far enough to tell code from comments and literals.

The audit reports vocabulary in *code position*, so this lexer has one
obligation: never let text inside a comment or a literal be mistaken for code,
and never let code be mistaken for trivia. A string is returned as one opaque
token rather than split into identifiers, so ``kani::assume`` inside a doc
comment or a string is not read as a call.

Rust has more literal forms than a ``split()`` understands, and each one has a
distinct terminator: ``r#"…"#`` ends at ``"#``, ``br##"…"##`` at ``"##``,
``c"…"`` is a C string rather than a byte string, and ``'a`` is a lifetime
while ``'a'`` is a character. :mod:`.literals` owns the string forms, so this
module and the decoder cannot disagree about where a literal ends. Every form
is modelled explicitly, and :class:`TrustAuditError` is raised rather than
guessed at when one never terminates.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import unicodedata

from rust_prover_tools.trust_audit.cursor import RUST_WHITESPACE, Cursor
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.literals import (
    consume_escaped_body,
    consume_quote_literal,
)
from rust_prover_tools.trust_audit.shebang import skip_shebang

__all__ = (
    "Kind",
    "Token",
    "tokenize",
)


class Kind(enum.StrEnum):
    """Distinguish an identifier, one opaque marker, and one whole literal.

    ``PUNCT`` carries a punctuator as well as a token the scan reads as opaque:
    a numeric literal and a character literal or lifetime are emitted under it
    so their text never reaches the identifier vocabulary or the string
    decoder.  What matters is that the token stays in the stream, keeping the
    tokens around it non-adjacent exactly as Rust lexes them.
    """

    IDENT = "ident"
    PUNCT = "punct"
    LITERAL = "literal"


@dc.dataclass(frozen=True, slots=True)
class Token:
    """One code token with its 1-based position; e.g. `assume` at line 7."""

    kind: Kind
    text: str
    line: int
    column: int
    #: Whether the author wrote the identifier in its raw form ``r#name``.  A
    #: raw name is never the keyword it spells, so syntax dispatch must ignore
    #: it even though vocabulary matching still reads its bare text.
    raw: bool = False


# Multi-character punctuators, longest first.
_PUNCTUATORS = (
    "::",
    "->",
    "=>",
    "..=",
    "..",
    "&&",
    "||",
    "::<",
    "!=",
    "==",
    "<=",
    ">=",
)


def tokenize(text: str) -> tuple[Token, ...]:
    """Return the code tokens of one Rust source.

    Parameters
    ----------
    text : str
        Rust source text, which need not be a complete or valid crate.

    Returns
    -------
    tuple[Token, ...]
        Tokens in source order. A comment contributes nothing, and a string is
        one opaque `Kind.LITERAL` token carrying its content, so a vocabulary
        word inside either is never split into an identifier.

    Raises
    ------
    TrustAuditError
        If a block comment, string, raw string, or character literal never
        terminates. The scan fails closed rather than resuming mid-literal,
        because a resumed scan reads the literal's contents as code.
    """
    # Rust strips one leading BOM before it looks for a shebang, and the BOM is
    # not otherwise part of the source text, so removing it keeps reported
    # columns aligned with the characters an author sees.
    cursor = Cursor(text.removeprefix("﻿"))
    skip_shebang(cursor)
    tokens: list[Token] = []
    while not cursor.at_end:
        if _skip_trivia(cursor):
            continue
        literal = consume_quote_literal(cursor)
        if literal is not None:
            tokens.append(
                Token(Kind.LITERAL, literal, cursor.line_before, cursor.column_before),
            )
            _skip_literal_suffix(cursor)
            continue
        opaque = _character_token(cursor) or _numeric_token(cursor)
        if opaque is None:
            opaque = _identifier_token(cursor)
        if opaque is not None:
            tokens.append(opaque)
            continue
        # The position is read before consuming, because ``_punct`` advances the
        # cursor past the token it returns. Reading afterwards would report a
        # punctuator's end, so an unclosed group would name the column after the
        # bracket the reader has to fix.
        line, column = cursor.line, cursor.column
        tokens.append(Token(Kind.PUNCT, _punct(cursor), line, column))
    return tuple(tokens)


def _skip_literal_suffix(cursor: Cursor) -> None:
    """Consume the identifier suffix that belongs to the literal just read.

    Rust lexes ``"text"suffix`` as one literal token.  Leaving the suffix in
    the stream would expose ``include`` in a token-tree input such as
    ``ignore!("text"include!())``, where it belongs to the literal rather than
    invoking ``include!``.  The suffix is not part of the quote-delimited span
    the decoder reads, so its text is consumed and not kept.
    """
    if cursor.at_end:
        return
    length = _plain_identifier_length(cursor.text, cursor.index)
    if length:
        cursor.take(length)


def _skip_trivia(cursor: Cursor) -> bool:
    """Consume whitespace and comments; report whether anything was consumed."""
    started = cursor.index
    while not cursor.at_end:
        if cursor.text[cursor.index] in RUST_WHITESPACE:
            cursor.take(1)
        elif cursor.starts("//"):
            newline = cursor.text.find("\n", cursor.index)
            end = len(cursor.text) if newline < 0 else newline
            cursor.take(end - cursor.index)
        elif cursor.starts("/*"):
            _skip_block_comment(cursor)
        else:
            break
    return cursor.index != started


def _skip_block_comment(cursor: Cursor) -> None:
    """Consume a block comment, honouring Rust's nesting rule.

    Nesting is why a naive ``find('*/')`` is wrong: in ``/* a /* b */ c */`` it
    stops inside the comment, and everything after ``c`` is then read as code.
    """
    start_line, start_column = cursor.line, cursor.column
    cursor.take(2)
    depth = 1
    while depth:
        if cursor.at_end:
            msg = (
                "unterminated block comment opened at line "
                f"{start_line}, column {start_column}"
            )
            raise TrustAuditError(msg)
        if cursor.starts("/*"):
            cursor.take(2)
            depth += 1
        elif cursor.starts("*/"):
            cursor.take(2)
            depth -= 1
        else:
            cursor.take(1)


def _character_token(cursor: Cursor) -> Token | None:
    """Consume a character literal or lifetime, including a byte prefix.

    ``'a`` is a lifetime and ``'a'`` a character, while ``b'x'`` is a byte
    literal.  Each is one opaque token: dropping it would make the tokens on
    either side adjacent, and the audit reads adjacency to recognise syntax
    Rust never lexed.
    """
    start, line, column = cursor.index, cursor.line, cursor.column
    if cursor.starts("b'"):
        # Lexing the ``b`` as an identifier would expose a local name the
        # compiler never sees.
        cursor.take(1)
    elif not cursor.starts("'"):
        return None
    if not _skip_character_literal_or_lifetime(cursor):
        return None
    return Token(Kind.PUNCT, cursor.text[start : cursor.index], line, column)


def _numeric_token(cursor: Cursor) -> Token | None:
    """Return one numeric literal as an opaque token, or ``None``.

    A numeric suffix may hold a vocabulary word: Rust lexes ``1assume`` as one
    literal, so emitting ``1`` and ``assume`` would invent a trust use.  The
    token is a punctuator rather than a literal because a proc-macro scan
    decodes literal text as code.
    """
    length = _numeric_literal_length(cursor.text, cursor.index)
    if not length:
        return None
    line, column = cursor.line, cursor.column
    return Token(Kind.PUNCT, cursor.take(length), line, column)


def _identifier_token(cursor: Cursor) -> Token | None:
    """Return one identifier token, or ``None``.

    The bare name is what the vocabulary matches, and Rust compares identifiers
    by their NFC forms, so the text is normalized once here rather than at
    every comparison.  The column stays at the prefix so a location names the
    token the author wrote.
    """
    length = _identifier_length(cursor.text, cursor.index)
    if not length:
        return None
    line, column = cursor.line, cursor.column
    written = cursor.take(length)
    raw = written.startswith("r#")
    name = unicodedata.normalize("NFC", written.removeprefix("r#"))
    return Token(Kind.IDENT, name, line, column, raw=raw)


def _skip_character_literal_or_lifetime(cursor: Cursor) -> bool:
    """Consume a character literal or a lifetime; report whether one started.

    ``'a`` is a lifetime and ``'a'`` is a character, and they differ by one
    quote. Reading a character literal as a lifetime would leave the closing
    quote to start a *new* literal and swallow the following code, so the two
    are separated here rather than left to a later stage.
    """
    if not cursor.starts("'"):
        return False
    cursor.line_before, cursor.column_before = cursor.line, cursor.column
    following = cursor.text[cursor.index + 1 : cursor.index + 2]
    if following == "\\":
        cursor.take(1)
        consume_escaped_body(
            cursor,
            "'",
            "character literal",
            line=cursor.line_before,
            column=cursor.column_before,
        )
        _skip_literal_suffix(cursor)
        return True
    if len(cursor.text) >= cursor.index + 3 and cursor.text[cursor.index + 2] == "'":
        cursor.take(3)
        _skip_literal_suffix(cursor)
        return True
    cursor.take(1)
    length = _identifier_length(cursor.text, cursor.index)
    if not length:
        # A quote that starts neither a character nor a lifetime is an
        # unterminated literal, and reading on would let the text it should
        # have contained pass for code.
        msg = (
            f"unterminated character literal at line {cursor.line}, "
            f"column {cursor.column - 1}"
        )
        raise TrustAuditError(msg)
    cursor.take(length)
    return True


def _numeric_literal_length(text: str, start: int) -> int:
    """Return the length of the numeric literal at ``start``, or zero.

    The audit reads no numeric value, but an arbitrary suffix belongs to the
    literal: Rust lexes ``1assume`` as one token, so splitting it would expose
    ``assume`` as a trust word the compiler never sees.  Consuming the leading
    digits and alphanumeric suffix as one opaque token is enough for that
    purpose; the remaining punctuation is lexed normally.
    """
    if start >= len(text) or not "0" <= text[start] <= "9":
        return 0
    end = start + 1
    while end < len(text) and _is_identifier_continue(text[end]):
        end += 1
    return end - start


def _identifier_length(text: str, start: int) -> int:
    """Return the length of the identifier at ``start``, or zero if none begins.

    Rust's raw form ``r#name`` counts as one identifier, so its prefix is part
    of the length; the caller strips the prefix from the token's text. The
    prefixed form is tried first and the plain form second, so an ``r`` that is
    not followed by a name still lexes as the identifier it is.
    """
    if text.startswith("r#", start):
        length = _plain_identifier_length(text, start + 2)
        if length:
            return 2 + length
    return _plain_identifier_length(text, start)


def _plain_identifier_length(text: str, start: int) -> int:
    """Return the length of the un-prefixed identifier at ``start``."""
    if start >= len(text) or not _is_identifier_start(text[start]):
        return 0
    end = start + 1
    while end < len(text) and _is_identifier_continue(text[end]):
        end += 1
    return end - start


def _is_identifier_start(character: str) -> bool:
    """Report whether ``character`` can begin a Rust identifier.

    Rust identifiers are Unicode XID: an ``XID_Start`` character, plus ``_``.
    Python's :meth:`str.isidentifier` applies exactly that rule to a
    single-character string, so it is the authority here rather than a
    handwritten category list.  A list misses characters such as the middle
    dot (U+00B7), which is an ``XID_Continue`` character Rust accepts in a
    module name; missing it splits ``mod a·b;`` and loses the file the
    declaration compiles.
    """
    return character.isidentifier()


def _is_identifier_continue(character: str) -> bool:
    """Report whether ``character`` can continue a Rust identifier.

    Python accepts a character in a non-initial position under the same XID
    rules Rust uses, so asking it about a two-character string answers the
    continuation question.
    """
    return ("a" + character).isidentifier()


def _punct(cursor: Cursor) -> str:
    """Consume one punctuator, preferring the longest form."""
    for candidate in _PUNCTUATORS:
        if cursor.starts(candidate):
            cursor.take(len(candidate))
            return candidate
    return cursor.take(1)
