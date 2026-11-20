"""Advance over one source string, tracking 1-based line and column.

Both the lexer and the literal decoder walk source text a character at a time,
and both must refuse an unterminated literal at the position where it started.
Keeping that arithmetic in one place means the two report the same position for
the same mistake.
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.errors import TrustAuditError

__all__ = ("RUST_WHITESPACE", "Cursor")

# Rust's lexical whitespace. `str.isspace` is wider (it also matches U+001C to
# U+001F, which Rust treats as ordinary characters), so the set is spelled out.
RUST_WHITESPACE: frozenset[str] = frozenset(
    "\t\n\v\f\r \x85\u200e\u200f\u2028\u2029",
)


class Cursor:
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
