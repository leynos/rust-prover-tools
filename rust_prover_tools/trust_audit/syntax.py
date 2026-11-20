"""Lex Rust far enough to tell code from comments and literals.

The audit reports vocabulary in *code position*, so this lexer has one
obligation: never let text inside a comment or a literal be mistaken for code,
and never let code be mistaken for trivia. A string is returned as one opaque
token rather than split into identifiers, so ``kani::assume`` inside a doc
comment or a string is not read as a call.

Rust has more literal forms than a ``split()`` understands, and each one has a
distinct terminator: ``r#"…"#`` ends at ``"#``, ``br##"…"##`` at ``"##``,
``c"…"`` is a C string rather than a byte string, and ``'a`` is a lifetime
while ``'a'`` is a character. Getting this wrong fails in the worst direction:
the lexer stops short of the real terminator, resumes mid-literal, and reports
the literal's contents as code. Each form is therefore modelled explicitly, and
:class:`TrustAuditError` is raised rather than guessed at when one never
terminates.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import re

__all__ = ("Kind", "Token", "TrustAuditError", "literal_body", "tokenize")


class TrustAuditError(Exception):
    """Report source the audit refuses to read."""


class Kind(enum.StrEnum):
    """Distinguish an identifier, one punctuator, and one whole literal."""

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


# A raw string's opening fence: `r"`, `r#"`, `br##"`, `cr"`, and so on.
_RAW_START = re.compile(r'(?:b|c)?r(#*)"')
# A byte or C string's prefix and opening quote, tried only without a raw fence.
_PREFIXED_QUOTE = re.compile(r'(?:b|c)"')
# An identifier, with Rust's raw form `r#name` folded in. `_RAW_START` is tried
# first, so this alternative only sees `r#` when no quote follows; without it
# `kani::r#assume(x)` lexes as `r`, `#`, `assume`, and the `#` standing between
# `::` and `assume` hides the call from the qualified-path rule.
_IDENT = re.compile(r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*")
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


class _Cursor:
    """Advance over one source string, tracking 1-based line and column."""

    def __init__(self, text: str) -> None:
        """Start at the first character of ``text``, on line 1, column 1."""
        self.text = text
        self.index = 0
        self.line = 1
        self.column = 1
        # A literal's token is positioned where it starts, before it is consumed.
        self.line_before = 1
        self.column_before = 1

    @property
    def at_end(self) -> bool:
        """Report whether every character has been consumed."""
        return self.index >= len(self.text)

    def starts(self, prefix: str) -> bool:
        """Report whether the source at the cursor begins with ``prefix``."""
        return self.text.startswith(prefix, self.index)

    def take(self, count: int) -> str:
        """Consume ``count`` characters and return them, updating the position."""
        chunk = self.text[self.index : self.index + count]
        for character in chunk:
            if character == "\n":
                self.line, self.column = self.line + 1, 1
            else:
                self.column += 1
        self.index += count
        return chunk

    def skip_until(self, terminator: str, what: str) -> None:
        """Consume through ``terminator``; refuse to continue if it is absent."""
        end = self.text.find(terminator, self.index)
        if end < 0:
            msg = f"unterminated {what} at line {self.line}, column {self.column}"
            raise TrustAuditError(msg)
        self.take(end + len(terminator) - self.index)


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
    cursor = _Cursor(text)
    tokens: list[Token] = []
    while not cursor.at_end:
        if _skip_trivia(cursor):
            continue
        literal = _consume_quote_literal(cursor)
        if literal is not None:
            tokens.append(
                Token(Kind.LITERAL, literal, cursor.line_before, cursor.column_before),
            )
            continue
        matched = _IDENT.match(cursor.text, cursor.index)
        if matched:
            # The bare name is what the vocabulary matches; the column stays at
            # the prefix so a location names the token the author wrote.
            tokens.append(
                Token(
                    Kind.IDENT,
                    matched[0].removeprefix("r#"),
                    cursor.line,
                    cursor.column,
                ),
            )
            cursor.take(len(matched[0]))
            continue
        if _skip_character_literal_or_lifetime(cursor):
            continue
        # The position is read before consuming, because ``_punct`` advances the
        # cursor past the token it returns. Reading afterwards would report a
        # punctuator's end, so an unclosed group would name the column after the
        # bracket the reader has to fix.
        line, column = cursor.line, cursor.column
        tokens.append(Token(Kind.PUNCT, _punct(cursor), line, column))
    return tuple(tokens)


def literal_body(source: str) -> tuple[int, str]:
    """Return a string literal's content offset and content.

    Parameters
    ----------
    source : str
        A literal token's text, as returned by `tokenize`.

    Returns
    -------
    tuple[int, str]
        The content's offset within ``source`` and the content itself, so a
        caller can report a position inside the literal exactly.
        ``literal_body('r#"a"#')`` is ``(3, 'a')`` and ``literal_body('"a"')``
        is ``(1, 'a')``.

    Raises
    ------
    TrustAuditError
        If ``source`` is not a string literal, which would mean a caller has
        passed the wrong token kind.
    """
    matched = _RAW_START.match(source)
    if matched:
        # A raw string's closing fence is the opening fence without its leading
        # prefix: `r#"` closes with `"#`. Measuring the symmetric `"#"` would
        # drop the body's last character.
        terminator = f'"{matched[1]}'
        return matched.end(), source[matched.end() : len(source) - len(terminator)]
    for prefix in ('b"', 'c"'):
        if source.startswith(prefix):
            return len(prefix), source[len(prefix) : -1]
    if source.startswith('"'):
        return 1, source[1:-1]
    msg = f"{source!r} is not a string literal"
    raise TrustAuditError(msg)


def _skip_trivia(cursor: _Cursor) -> bool:
    """Consume whitespace and comments; report whether anything was consumed."""
    started = cursor.index
    while not cursor.at_end:
        if cursor.text[cursor.index] in " \t\r\n":
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


def _skip_block_comment(cursor: _Cursor) -> None:
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


def _consume_quote_literal(cursor: _Cursor) -> str | None:
    """Consume a raw, byte, or C string and return its whole source span.

    The span keeps the prefix and fences so that `literal_body` can both
    recover the content and locate it inside the token. ``None`` means no
    string starts at the cursor, which is how the caller tells a literal from
    an identifier.
    """
    cursor.line_before, cursor.column_before = cursor.line, cursor.column
    start = cursor.index
    matched = _RAW_START.match(cursor.text, cursor.index)
    if matched:
        # A raw string closes with the same number of `#` it opened with:
        # `r#"…"#` ends at `"#`, and `r##"…"##` at `"##`.
        fence = f'"{matched[1]}'
        cursor.take(matched.end() - start)
        cursor.skip_until(fence, "raw string")
        return cursor.text[start : cursor.index]
    # `_PREFIXED_QUOTE` matches the prefix *and* the opening quote, so its whole
    # match is the opening. Measuring the `b`/`c` alone and adding one would
    # consume the quote of an empty `b""` twice, and the body would then run on
    # to the next string in the file.
    opening = _PREFIXED_QUOTE.match(cursor.text, cursor.index)
    if opening or cursor.starts('"'):
        what = "byte or C string" if opening else "string"
        cursor.take(len(opening[0]) if opening else 1)
        _consume_escaped_body(cursor, '"', what)
        return cursor.text[start : cursor.index]
    return None


def _consume_escaped_body(cursor: _Cursor, terminator: str, what: str) -> str:
    r"""Consume through the closing quote and return the body, escapes included.

    A backslash escapes exactly one following character, which keeps ``"a\\""``
    from ending at the escaped quote. The escape is returned verbatim because no
    caller interprets the text; the terminator is what matters here.

    The failure names the literal's *opening* line and column: by the time the
    end is reached the cursor sits at EOF, and reporting that position would
    point at the end of the file instead of the mistake.
    """
    start, start_line, start_column = cursor.index, cursor.line, cursor.column
    while not cursor.at_end:
        if cursor.starts("\\"):
            cursor.take(2)
        elif cursor.starts(terminator):
            body = cursor.text[start : cursor.index]
            cursor.take(1)
            return body
        else:
            cursor.take(1)
    msg = f"unterminated {what} at line {start_line}, column {start_column}"
    raise TrustAuditError(msg)


def _skip_character_literal_or_lifetime(cursor: _Cursor) -> bool:
    """Consume a character literal or a lifetime; report whether one started.

    ``'a`` is a lifetime and ``'a'`` is a character, and they differ by one
    quote. Reading a character literal as a lifetime would leave the closing
    quote to start a *new* literal and swallow the following code, so the two
    are separated here rather than left to a later stage.
    """
    if not cursor.starts("'"):
        return False
    following = cursor.text[cursor.index + 1 : cursor.index + 2]
    if following == "\\":
        cursor.take(1)
        _consume_escaped_body(cursor, "'", "character literal")
        return True
    if len(cursor.text) >= cursor.index + 3 and cursor.text[cursor.index + 2] == "'":
        cursor.take(3)
        return True
    cursor.take(1)
    matched = _IDENT.match(cursor.text, cursor.index)
    if matched:
        cursor.take(len(matched[0]))
    return True


def _punct(cursor: _Cursor) -> str:
    """Consume one punctuator, preferring the longest form."""
    for candidate in _PUNCTUATORS:
        if cursor.starts(candidate):
            cursor.take(len(candidate))
            return candidate
    return cursor.take(1)
