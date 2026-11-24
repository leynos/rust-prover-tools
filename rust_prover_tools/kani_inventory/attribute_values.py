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

from rust_prover_tools.kani_inventory.models import InventoryDiscoveryError

__all__ = (
    "DEPTH_DELTAS",
    "PROOF_ATTRIBUTES",
    "attribute_name",
    "cfg_attr_facts",
    "inner_gates",
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

PROOF_ATTRIBUTES = frozenset({
    "kani::proof",
    "kani::proof_for_contract",
    "::kani::proof",
    "::kani::proof_for_contract",
})
_RAW_STRING = re.compile(r'r(?P<hashes>#*)"(?P<body>.*?)"(?P=hashes)', re.DOTALL)
_HEX_DIGITS = 2
_MIN_CFG_ATTR_PARTS = 2
_QUOTE_PAIR = 2
_MAX_UNICODE_DIGITS = 6
_MAX_CODE_POINT = 0x10FFFF
_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "'": "'",
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


def _strip_comments(text: str) -> str:
    """Remove Rust comments while leaving string literals intact."""
    out: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            index = _line_comment_end(text, index)
            continue
        if text.startswith("/*", index):
            index = _block_comment_end(text, index)
            continue
        copied = _copy_literal(out, text, index)
        if copied != index:
            index = copied
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _line_comment_end(text: str, index: int) -> int:
    """Return the position after one line comment."""
    end = text.find("\n", index)
    return len(text) if end < 0 else end


def _block_comment_end(text: str, index: int) -> int:
    """Return the position after one nested block comment."""
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
    return position


def _copy_literal(out: list[str], text: str, index: int) -> int:
    """Copy one string literal to `out`, returning its new position."""
    raw = _RAW_STRING.match(text, index)
    if raw is not None:
        boundary = index == 0 or not (
            text[index - 1].isalnum() or text[index - 1] == "_"
        )
        if boundary:
            out.append(raw.group(0))
            return raw.end()
    if text[index] != '"':
        return index
    start = index
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            index += 1
            break
        index += 1
    out.append(text[start:index])
    return index


def path_value(content: str) -> str | None:
    """Extract a Rust string value from a ``path = ...`` attribute."""
    _, separator, value = _strip_comments(content).partition("=")
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
        return None, _skip_continuation(text, index + 2)
    return None


def _skip_continuation(text: str, start: int) -> int:
    """Skip the whitespace Rust removes after an escaped newline."""
    position = start
    while position < len(text) and text[position].isspace():
        position += 1
    return position


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
    digits = text[start + 1 : close].replace("_", "")
    if not digits or len(digits) > _MAX_UNICODE_DIGITS:
        return None
    if not all(char in string.hexdigits for char in digits):
        return None
    value = int(digits, 16)
    if value > _MAX_CODE_POINT:
        return None
    return chr(value), close + 1


def inner_gates(masked: str) -> bool:
    """Whether an inner attribute can gate its enclosing module.

    An inner attribute applies to the module. ``cfg`` gates it directly;
    ``cfg_attr`` gates it only when one of its nested attributes can remove or
    gate the module, so a lint-only wrapper stays harmless.
    """
    name, _ = attribute_name(masked)
    if name == "cfg":
        return normalise(masked) != "cfg(kani)"
    if name == "cfg_attr":
        nested = cfg_attr_facts(masked, masked)
        return nested is not None and nested[1]
    return False


def cfg_attr_facts(
    masked: str,
    original: str,
) -> tuple[list[str], bool, str | None] | None:
    """Return nested proof facts, conditionality, and an active path."""
    parsed = _parse_cfg_attr(masked, original)
    if parsed is None:
        return None
    predicate, pairs = parsed
    return _fold_cfg_facts(predicate, pairs)


def _parse_cfg_attr(
    masked: str,
    original: str,
) -> tuple[str, list[tuple[str, str]]] | None:
    """Split one ``cfg_attr`` into its predicate and attribute parts."""
    name, arguments = attribute_name(masked)
    arguments = arguments.strip()
    if name != "cfg_attr" or not arguments.startswith("("):
        return None
    if not arguments.endswith(")"):
        return None
    start = masked.index("(") + 1
    close = masked.rfind(")")
    if close <= start:
        return None
    spans = _split_spans(masked[start:close])
    if len(spans) < _MIN_CFG_ATTR_PARTS:
        return None
    pairs = [
        (
            masked[start + left : start + right],
            original[start + left : start + right],
        )
        for left, right in spans
    ]
    return normalise(pairs[0][0]), pairs[1:]


def _fold_cfg_facts(
    predicate: str,
    pairs: list[tuple[str, str]],
) -> tuple[list[str], bool, str | None] | None:
    """Combine nested attribute facts under one predicate.

    The predicate gates the nested attributes, not the item, unless one of
    those attributes can itself remove or gate the item. A ``cfg_attr`` that
    wraps only a lint attribute such as ``allow(dead_code)`` therefore leaves
    the proof unconditional; one that wraps ``cfg(...)`` or a proof attribute
    makes it conditional.
    """
    proofs: list[str] = []
    gating = False
    path: str | None = None
    for masked_part, original_part in pairs:
        nested = _nested_cfg_attr_facts(masked_part, original_part)
        if nested is None:
            return None
        if nested[2] is not None:
            if predicate != "kani":
                msg = (
                    "a conditional module #[path] cannot be discovered "
                    "without evaluating its cfg predicate"
                )
                raise InventoryDiscoveryError(msg)
            path = nested[2]
        proofs.extend(nested[0])
        gating = gating or nested[1]
    # A nested cfg gates the item whenever the outer predicate holds, which
    # `kani` does during a verification. A proof attribute is gated by the
    # outer predicate instead, so only a non-kani predicate makes it
    # conditional.
    conditional = gating or (bool(proofs) and predicate != "kani")
    return proofs, conditional, path


def _nested_cfg_attr_facts(
    masked: str,
    original: str,
) -> tuple[list[str], bool, str | None] | None:
    """Return the facts one attribute inside a ``cfg_attr`` contributes."""
    nested_name, _ = attribute_name(masked)
    if nested_name == "cfg_attr":
        return cfg_attr_facts(masked, original)
    if nested_name in PROOF_ATTRIBUTES:
        return [nested_name], False, None
    if nested_name == "path":
        return [], False, path_value(original)
    if nested_name == "cfg" and normalise(masked) != "cfg(kani)":
        return [], True, None
    return [], False, None


def _split_spans(content: str) -> list[tuple[int, int]]:
    """Return top-level comma-separated spans in `content`."""
    spans: list[tuple[int, int]] = []
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
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(content)))
    return spans
