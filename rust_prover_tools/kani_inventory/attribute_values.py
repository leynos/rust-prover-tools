"""Parse the attribute values and nested attributes discovery depends on.

Rust attribute syntax is a small token language: string literals may be raw or
escaped, and `cfg_attr` expands another attribute only when its predicate
holds. Keeping these rules together lets the item scanner read `path` values
and proof attributes without treating a comment, an ordinary string, or an
unexpanded predicate as code.
"""

from __future__ import annotations

import re
import string

__all__ = (
    "DEPTH_DELTAS",
    "PROOF_ATTRIBUTES",
    "attribute_name",
    "cfg_attr_facts",
    "normalise",
    "path_value",
)

DEPTH_DELTAS = {
    "{": (1, 0, 0),
    "}": (-1, 0, 0),
    "(": (0, 1, 0),
    ")": (0, -1, 0),
    "[": (0, 0, 1),
    "]": (0, 0, -1),
}

PROOF_ATTRIBUTES = frozenset({"kani::proof", "kani::proof_for_contract"})
_RAW_STRING = re.compile(r'r(?P<hashes>#*)"(?P<body>.*)"(?P=hashes)', re.DOTALL)
_HEX_DIGITS = 2
_MIN_CFG_ATTR_PARTS = 2
_QUOTE_PAIR = 2
_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
}


def normalise(content: str) -> str:
    """Remove whitespace so equivalent attribute spellings compare equal."""
    return re.sub(r"\s+", "", content)


def attribute_name(content: str) -> tuple[str, str]:
    """Return an attribute's path and its argument text.

    Rust attributes are token sequences, not fixed spellings: whitespace may
    surround ``::`` and a path may carry arguments. The path is everything
    before the first top-level ``(`` or ``=``, with all whitespace removed.
    """
    depth = 0
    for index, char in enumerate(content):
        if char == "(" and depth == 0:
            return normalise(content[:index]), content[index:]
        if char == "=" and depth == 0:
            return normalise(content[:index]), content[index:]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    return normalise(content), ""


def path_value(content: str) -> str | None:
    """Extract a Rust string value from a ``path = ...`` attribute."""
    _, separator, value = content.partition("=")
    if not separator:
        return None
    literal = value.strip()
    raw = _RAW_STRING.fullmatch(literal)
    if raw is not None:
        return raw["body"]
    if len(literal) >= _QUOTE_PAIR and literal[0] == literal[-1] == '"':
        return _unescape_rust(literal[1:-1])
    return None


def _unescape_rust(text: str) -> str | None:
    """Decode the escapes a Rust string literal permits, or return `None`."""
    decoded: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            decoded.append(text[index])
            index += 1
            continue
        step = _rust_escape(text, index)
        if step is None:
            return None
        value, index = step
        if value is not None:
            decoded.append(value)
    return "".join(decoded)


def _rust_escape(text: str, index: int) -> tuple[str | None, int] | None:
    """Decode one escape at `index`, or return `None` when unsupported."""
    if index + 1 >= len(text):
        return None
    escape = text[index + 1]
    if escape in _ESCAPES:
        return _ESCAPES[escape], index + 2
    if escape == "x":
        return _hex_escape(text, index + 2)
    if escape == "u":
        return _unicode_escape(text, index + 2)
    if escape == "\n":
        return None, index + 2
    return None


def _hex_escape(text: str, start: int) -> tuple[str, int] | None:
    r"""Decode ``\xNN`` beginning at `start`."""
    digits = text[start : start + _HEX_DIGITS]
    if len(digits) != _HEX_DIGITS:
        return None
    if not all(char in string.hexdigits for char in digits):
        return None
    return chr(int(digits, 16)), start + _HEX_DIGITS


def _unicode_escape(text: str, start: int) -> tuple[str, int] | None:
    r"""Decode ``\u{...}`` beginning at `start`."""
    if not text.startswith("{", start):
        return None
    close = text.find("}", start + 1)
    if close < 0:
        return None
    digits = text[start + 1 : close]
    if not digits:
        return None
    if not all(char in string.hexdigits for char in digits):
        return None
    return chr(int(digits, 16)), close + 1


def cfg_attr_facts(masked: str) -> tuple[list[str], bool] | None:
    """Return proof attributes nested in ``cfg_attr`` and their conditionality."""
    content = masked.strip()
    prefix = "cfg_attr("
    if not content.startswith(prefix) or not content.endswith(")"):
        return None
    parts = _split_top_level(content[len(prefix) : -1])
    if len(parts) < _MIN_CFG_ATTR_PARTS:
        return None
    predicate = normalise(parts[0])
    proofs: list[str] = []
    conditional = False
    for part in parts[1:]:
        nested_name, _ = attribute_name(part)
        if nested_name in PROOF_ATTRIBUTES:
            proofs.append(nested_name)
            if predicate != "kani":
                conditional = True
        elif nested_name == "cfg" and normalise(part) != "cfg(kani)":
            conditional = True
    return proofs, conditional


def _split_top_level(content: str) -> list[str]:
    """Split comma-separated text at the top level only."""
    parts: list[str] = []
    start = 0
    depth = [0, 0, 0]
    for index, char in enumerate(content):
        delta = DEPTH_DELTAS.get(char)
        if delta is not None:
            depth = [
                max(0, value + change)
                for value, change in zip(depth, delta, strict=True)
            ]
        if char == "," and depth == [0, 0, 0]:
            parts.append(content[start:index])
            start = index + 1
    parts.append(content[start:])
    return parts
