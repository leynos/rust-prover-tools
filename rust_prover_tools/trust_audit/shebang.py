"""Decide whether a leading ``#!`` line is a shebang or an attribute.

Rust discards a shebang before lexing, unless the next significant token is
``[``, which opens an inner attribute.  The lookahead has to skip ordinary
whitespace and comments but stop at a doc comment: rustc expands a doc comment
into an attribute, so it cannot stand between ``#!`` and an inner ``[``.

Keeping this rule apart from the token loop means the lexer never mistakes a
shebang's interpreter arguments for Rust source.
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.cursor import RUST_WHITESPACE, Cursor

__all__ = ("skip_shebang",)


def skip_shebang(cursor: Cursor) -> None:
    """Consume a leading shebang, which Rust discards before lexing.

    Rust treats a first line beginning ``#!`` as a shebang unless the next
    significant token is ``[``, which makes it an inner attribute.  Whitespace
    and ordinary comments may sit between the two; a doc comment may not,
    because rustc expands it into an attribute.  The shebang line is not Rust
    code, so an apostrophe in the interpreter arguments must not open a
    character literal and a quote there must not open a string the file never
    contains.
    """
    if not cursor.starts("#!"):
        return
    if _next_code_character(cursor.text, cursor.index + 2) == "[":
        return
    newline = cursor.text.find("\n", cursor.index)
    end = len(cursor.text) if newline < 0 else newline
    cursor.take(end - cursor.index)


def _next_code_character(text: str, index: int) -> str | None:
    """Return the next significant character, or ``None`` at the end.

    Only the decision between a shebang and an inner attribute needs this
    lookahead; the cursor is not moved.  A doc comment is significant: rustc
    expands it into an attribute, so it does not stand between ``#!`` and an
    inner ``[`` the way an ordinary comment does.
    """
    while index < len(text):
        if text[index] in RUST_WHITESPACE:
            index += 1
        elif text.startswith("//", index):
            if _is_doc_line_comment(text, index):
                return text[index]
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        elif text.startswith("/*", index):
            if _is_doc_block_comment(text, index):
                return text[index]
            index = _after_block_comment(text, index)
        else:
            return text[index]
    return None


def _is_doc_line_comment(text: str, index: int) -> bool:
    """Report whether the line comment at ``index`` is a doc comment.

    ``//!`` is an inner doc comment and ``///`` an outer one; ``////`` with
    four or more slashes is an ordinary comment, matching rustc's lexer.
    """
    third = text[index + 2 : index + 3]
    if third == "!":
        return True
    return third == "/" and text[index + 3 : index + 4] != "/"


def _is_doc_block_comment(text: str, index: int) -> bool:
    """Report whether the block comment at ``index`` is a doc comment.

    ``/*!`` and ``/**`` are doc comments; ``/***`` and ``/**/`` are not, which
    is the distinction rustc's lexer draws.
    """
    third = text[index + 2 : index + 3]
    if third == "!":
        return True
    return third == "*" and text[index + 3 : index + 4] not in {"*", "/"}


def _after_block_comment(text: str, index: int) -> int:
    """Return the index after the block comment opening at ``index``.

    Rust nests block comments, so the scan counts openers rather than stopping
    at the first ``*/``.
    """
    depth = 0
    position = index
    while position < len(text):
        if text.startswith("/*", position):
            depth += 1
            position += 2
        elif text.startswith("*/", position):
            depth -= 1
            position += 2
            if depth == 0:
                return position
        else:
            position += 1
    return len(text)
