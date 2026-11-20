r"""Find and decode Rust string literals.

Rust has more literal forms than a ``split()`` understands, and each one has a
distinct terminator: ``r#"…"#`` ends at ``"#``, ``br##"…"##`` at ``"##``,
``c"…"`` is a C string rather than a byte string. The lexer uses the opening
fences here to find the end of a token, and a caller uses the decoder below to
read the value that token names: ``#[path = "\x63hild.rs"]`` compiles
``child.rs``, so looking up the source spelling would read a different file, or
none at all.
"""

from __future__ import annotations

import re
import string
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError

if typ.TYPE_CHECKING:
    from rust_prover_tools.trust_audit.cursor import Cursor

__all__ = (
    "PREFIXED_QUOTE",
    "RAW_START",
    "consume_escaped_body",
    "consume_quote_literal",
    "decoded_literal_body",
    "decoded_literal_body_positions",
    "literal_body",
)

# A raw string's opening fence: `r"`, `r#"`, `br##"`, `cr"`, and so on.
RAW_START = re.compile(r'(?:b|c)?r(#*)"')
# A byte or C string's prefix and opening quote, tried only without a raw fence.
PREFIXED_QUOTE = re.compile(r'(?:b|c)"')
# The escapes a cooked Rust string may carry, apart from its numeric forms.
_ESCAPED_CHARACTERS = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    "0": "\0",
    "'": "'",
    '"': '"',
}
_HEX_DIGITS = frozenset(string.hexdigits)
_HEX_ESCAPE_DIGITS = 2
# Rust allows at most six hexadecimal digits in a `\u{…}` escape, however many
# separators they are written with.
_MAX_UNICODE_DIGITS = 6
# A `\u{…}` escape names a Unicode scalar value; surrogates are code points but
# not scalars, and Rust rejects them where Python's `chr` would accept one.
_SURROGATES = range(0xD800, 0xE000)
_MAX_SCALAR = 0x10FFFF


def consume_quote_literal(cursor: Cursor) -> str | None:
    """Consume a raw, byte, or C string and return its whole source span.

    The span keeps the prefix and fences so that `literal_body` can both
    recover the content and locate it inside the token. ``None`` means no
    string starts at the cursor, which is how the caller tells a literal from
    an identifier.
    """
    cursor.line_before, cursor.column_before = cursor.line, cursor.column
    start = cursor.index
    matched = RAW_START.match(cursor.text, cursor.index)
    if matched:
        # A raw string closes with the same number of `#` it opened with:
        # `r#"…"#` ends at `"#`, and `r##"…"##` at `"##`.
        fence = f'"{matched[1]}'
        cursor.take(matched.end() - start)
        cursor.skip_until(fence, "raw string")
        return cursor.text[start : cursor.index]
    # `PREFIXED_QUOTE` matches the prefix *and* the opening quote, so its whole
    # match is the opening. Measuring the `b`/`c` alone and adding one would
    # consume the quote of an empty `b""` twice, and the body would then run on
    # to the next string in the file.
    opening = PREFIXED_QUOTE.match(cursor.text, cursor.index)
    if opening or cursor.starts('"'):
        what = "byte or C string" if opening else "string"
        cursor.take(len(opening[0]) if opening else 1)
        consume_escaped_body(cursor, '"', what)
        return cursor.text[start : cursor.index]
    return None


def consume_escaped_body(cursor: Cursor, terminator: str, what: str) -> str:
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
    matched = RAW_START.match(source)
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


def decoded_literal_body(source: str) -> tuple[int, str]:
    r"""Return a string literal's content offset and its Rust-decoded value.

    Rust reads the escapes in a cooked literal, so ``#[path = "\x63hild.rs"]``
    names ``child.rs``. Looking up the source spelling instead would read a
    different file, or none at all. A raw literal has no escapes to read, so
    its body is returned as written. The offset is the body's start within
    ``source``, so a caller can still locate the value in its token.

    Parameters
    ----------
    source : str
        A literal token's text, as returned by `tokenize`.

    Returns
    -------
    tuple[int, str]
        The content's offset and its decoded value.
        ``decoded_literal_body('"a\\nb"')`` is ``(1, 'a\nb')``.

    Raises
    ------
    TrustAuditError
        If ``source`` is not a string literal, or a cooked literal carries an
        escape Rust would reject.
    """
    offset, body, _ = decoded_literal_body_positions(source)
    return offset, body


def decoded_literal_body_positions(
    source: str,
) -> tuple[int, str, tuple[int, ...]]:
    r"""Return a literal's decoded value and each character's source offset.

    A caller that reports where a decoded value matched needs the position of
    the characters it matched, not of the escape that produced them. Each
    decoded character is therefore paired with the offset, within ``source``,
    of the escape or literal character it came from. A line continuation
    produces no character, so it contributes no position.

    Parameters
    ----------
    source : str
        A literal token's text, as returned by `tokenize`.

    Returns
    -------
    tuple[int, str, tuple[int, ...]]
        The content's offset, its decoded value, and one source offset per
        decoded character. ``decoded_literal_body_positions('"\\x61"')`` is
        ``(1, 'a', (1,))``.

    Raises
    ------
    TrustAuditError
        If ``source`` is not a string literal, or a cooked literal carries an
        escape Rust would reject.
    """
    offset, body = literal_body(source)
    if RAW_START.match(source):
        return offset, body, tuple(range(offset, offset + len(body)))
    decoded, positions = _decode_escapes(body, source)
    return offset, decoded, tuple(offset + position for position in positions)


def _decode_escapes(body: str, source: str) -> tuple[str, tuple[int, ...]]:
    """Read one cooked body, pairing each decoded character with its source.

    The position is where the character was written, so a match report can
    point at the escape rather than at the decoded text it produced.
    """
    characters: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(body):
        if body[index] != "\\":
            characters.append(body[index])
            positions.append(index)
            index += 1
            continue
        start = index
        index = _decode_escape(body, index, characters, source)
        # However many characters an escape produced, all of them were written
        # as the one escape, so all of them point back at its backslash.
        positions.extend([start] * (len(characters) - len(positions)))
    return "".join(characters), tuple(positions)


def _decode_escape(
    body: str,
    index: int,
    decoded: list[str],
    source: str,
) -> int:
    """Decode the escape at ``index`` and return the index just past it."""
    if index + 1 >= len(body):
        msg = f"unterminated escape in {source!r}"
        raise TrustAuditError(msg)
    escape = body[index + 1]
    simple = _ESCAPED_CHARACTERS.get(escape)
    if simple is not None:
        decoded.append(simple)
        return index + 2
    if escape == "\n":
        # A line continuation skips the newline and the indentation after it.
        return _skip_continuation(body, index + 2)
    if escape == "x":
        return _decode_hex_escape(body, index, decoded, source)
    if escape == "u":
        return _decode_unicode_escape(body, index, decoded, source)
    msg = f"unknown escape \\{escape} in {source!r}"
    raise TrustAuditError(msg)


def _skip_continuation(body: str, index: int) -> int:
    """Return the index after the whitespace a line continuation swallows."""
    while index < len(body) and body[index] in " \t\r\n":
        index += 1
    return index


def _decode_hex_escape(
    body: str,
    index: int,
    decoded: list[str],
    source: str,
) -> int:
    r"""Decode the ``\xNN`` escape at ``index`` into one character."""
    start = index + 2
    digits = body[start : start + _HEX_ESCAPE_DIGITS]
    if len(digits) != _HEX_ESCAPE_DIGITS or not all(
        digit in _HEX_DIGITS for digit in digits
    ):
        msg = f"malformed \\x escape in {source!r}"
        raise TrustAuditError(msg)
    decoded.append(chr(int(digits, 16)))
    return start + _HEX_ESCAPE_DIGITS


def _decode_unicode_escape(
    body: str,
    index: int,
    decoded: list[str],
    source: str,
) -> int:
    r"""Decode the ``\u{…}`` escape at ``index`` into one character.

    Rust permits underscores between the hexadecimal digits — ``\u{6_3}`` is
    ``c`` — so the separators are removed before the digit count and the value
    are checked. The six-digit ceiling is Rust's, not Python's.
    """
    opening = index + 2
    if opening >= len(body) or body[opening] != "{":
        msg = f"malformed \\u escape in {source!r}"
        raise TrustAuditError(msg)
    closing = body.find("}", opening + 1)
    written = body[opening + 1 : closing] if closing >= 0 else ""
    digits = written.replace("_", "")
    if not _is_unicode_digits(digits):
        msg = f"malformed \\u escape in {source!r}"
        raise TrustAuditError(msg)
    decoded.append(_scalar_character(digits))
    return closing + 1


def _is_unicode_digits(digits: str) -> bool:
    """Report whether ``digits`` is a digit run Rust would accept.

    An empty run names no character at all, and the six-digit ceiling is
    Rust's own limit on how many digits an escape may carry. Each check is a
    separate guard so the reason a run was refused stays readable.
    """
    if not digits:
        return False
    if len(digits) > _MAX_UNICODE_DIGITS:
        return False
    return all(digit in _HEX_DIGITS for digit in digits)


def _scalar_character(digits: str) -> str:
    r"""Return the character one ``\u{…}`` escape names.

    Rust accepts only Unicode scalar values here, so a surrogate or a code
    point past the last one is refused rather than turned into a character the
    compiler would never have produced.
    """
    code = int(digits, 16)
    if code in _SURROGATES or code > _MAX_SCALAR:
        msg = f"\\u escape {digits} is not a Unicode scalar value"
        raise TrustAuditError(msg)
    return chr(code)
