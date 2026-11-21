"""Blank comments and string-like literals while preserving source layout.

A lexical scanner that reads raw Rust can mistake the word ``mod`` inside a
comment or a proof attribute inside a doc example for real code. Masking
replaces everything inside a comment or literal with spaces, but keeps every
newline, so positions and line numbers in the masked text still describe the
original file.

The masking is conservative: a lifetime is left untouched, because a lifetime
is code. A character literal is masked, because its content is data. A byte
string or raw string is masked from its prefix, so no quotation mark inside it
can start a false literal.
"""

from __future__ import annotations

__all__ = ("mask_source",)


def mask_source(text: str) -> str:
    """Replace comments and string-like literals with spaces.

    Parameters
    ----------
    text : str
        Rust source.

    Returns
    -------
    str
        The same length as `text`, with newlines preserved.
    """
    out = list(text)
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            index = _mask_line_comment(out, text, index)
        elif text.startswith("/*", index):
            index = _mask_block_comment(out, text, index)
        elif _starts_raw_string(text, index):
            index = _mask_raw_string(out, text, index)
        elif text[index] == '"':
            index = _mask_string(out, text, index)
        elif text[index] == "'":
            index = _mask_char_or_lifetime(out, text, index)
        else:
            index += 1
    return "".join(out)


def _mask_line_comment(out: list[str], text: str, index: int) -> int:
    """Mask a ``//`` comment and return the position after it."""
    end = text.find("\n", index)
    if end < 0:
        end = len(text)
    _blank(out, text, index, end)
    return end


def _mask_block_comment(out: list[str], text: str, index: int) -> int:
    """Mask a (possibly nested) ``/* */`` comment."""
    depth = 1
    position = index + 2
    while position < len(text) and depth:
        if text.startswith("/*", position):
            depth += 1
            position += 2
        elif text.startswith("*/", position):
            depth -= 1
            position += 2
        else:
            position += 1
    _blank(out, text, index, min(position, len(text)))
    return position


def _blank(out: list[str], text: str, start: int, end: int) -> None:
    """Replace `start`..`end` with spaces, keeping newlines."""
    for position in range(start, min(end, len(text))):
        if text[position] != "\n":
            out[position] = " "


def _is_identifier_char(char: str) -> bool:
    """Whether `char` can be part of a Rust identifier."""
    return char.isalnum() or char == "_"


def _starts_raw_string(text: str, index: int) -> bool:
    """Whether a raw-string prefix starts at `index`."""
    prefix = index + 1
    if text.startswith("br", index):
        prefix = index + 2
    elif not text.startswith("r", index):
        return False
    if index and _is_identifier_char(text[index - 1]):
        return False
    while prefix < len(text) and text[prefix] == "#":
        prefix += 1
    return prefix < len(text) and text[prefix] == '"'


def _mask_raw_string(out: list[str], text: str, index: int) -> int:
    """Mask a raw string, including its hashes."""
    hashes = 0
    position = index + 1
    if text.startswith("br", index):
        position = index + 2
    while position < len(text) and text[position] == "#":
        hashes += 1
        position += 1
    position += 1  # opening quote
    closing = '"' + "#" * hashes
    found = text.find(closing, position)
    end = len(text) if found < 0 else found + len(closing)
    _blank(out, text, index, end)
    return end


def _mask_string(out: list[str], text: str, index: int) -> int:
    """Mask a normal string literal, honouring escapes."""
    position = index + 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == '"':
            position += 1
            break
        position += 1
    _blank(out, text, index, min(position, len(text)))
    return position


def _mask_char_or_lifetime(out: list[str], text: str, index: int) -> int:
    """Mask a character literal, leaving a lifetime untouched."""
    position = index + 1
    if position >= len(text):
        return index + 1
    if text[position] == "\\":
        return _mask_escape_char(out, text, index)
    if position + 1 < len(text) and text[position + 1] == "'":
        _blank(out, text, index, position + 2)
        return position + 2
    return index + 1


def _mask_escape_char(out: list[str], text: str, index: int) -> int:
    r"""Mask an escaped character literal such as ``'\n'`` or ``'\''``."""
    position = index + 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == "'" and position > index:
            _blank(out, text, index, position + 1)
            return position + 1
        position += 1
    return index + 1
