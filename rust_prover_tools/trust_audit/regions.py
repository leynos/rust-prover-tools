"""Locate the token regions that carry Verus' own vocabulary.

A Verus word is a request for trust only where Verus gives it meaning: inside a
``verus!`` body, or in an attribute such as ``verifier``. This module answers
"which token ranges are those" once per file, so the matcher can ask a range
question rather than re-deriving the boundaries for every word it classifies.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.structure import (
    attribute_spans,
    matching,
    texts,
)
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.vocabulary import VERUS_ATTRIBUTE_HEADS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("verus_regions",)

# ``verus`` and ``!``: the head of a ``verus!`` invocation. The macro accepts
# the same items in any of Rust's three delimiter pairs, so all three are read.
_VERUS_MACRO_HEAD = ("verus", "!")
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}
_OPENERS = frozenset(_DELIMITER_PAIRS)
_CLOSERS = frozenset(_DELIMITER_PAIRS.values())


def verus_regions(tokens: tuple[Token, ...]) -> tuple[tuple[int, int], ...]:
    """Return the token ranges whose identifiers carry Verus' own vocabulary.

    Two forms create a region: a ``verus!`` body, and a Verus attribute anywhere
    in an attribute list, including one nested in a ``cfg_attr``.
    """
    regions = list(_verus_bodies(tokens))
    for open_index, close_index in attribute_spans(tokens):
        regions.extend(_verus_attribute_regions(tokens, open_index, close_index))
    return tuple(regions)


def _verus_bodies(tokens: tuple[Token, ...]) -> cabc.Iterator[tuple[int, int]]:
    """Locate each ``verus!`` body, tolerating nesting and any delimiter.

    The macro accepts the same items in braces, parentheses or brackets, so
    reading only ``verus! { … }`` would leave the body of ``verus!( … )``
    ordinary Rust and lose every Verus word in it.
    """
    head_length = len(_VERUS_MACRO_HEAD)
    for index in range(len(tokens) - head_length):
        if texts(tokens, index, head_length) != _VERUS_MACRO_HEAD:
            continue
        opening = index + head_length
        closer = _DELIMITER_PAIRS.get(tokens[opening].text)
        if closer is None:
            continue
        yield opening + 1, matching(tokens, opening, tokens[opening].text, closer)


def _verus_attribute_regions(
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
) -> tuple[tuple[int, int], ...]:
    """Return the range of an attribute whose identifiers are Verus vocabulary."""
    head_index = open_index + 1
    if head_index >= close_index:
        return ()
    if tokens[head_index].text in VERUS_ATTRIBUTE_HEADS:
        return ((head_index, close_index - 1),)
    if tokens[head_index].text != "cfg_attr":
        return ()
    return _cfg_attr_regions(tokens, head_index + 1, close_index)


def _cfg_attr_regions(
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
) -> tuple[tuple[int, int], ...]:
    """Return the range of each Verus attribute in a ``cfg_attr`` payload.

    Only the payload counts. ``cfg_attr(all(verifier, external), inline)``
    applies ``inline``: its predicate merely asks whether the ``verifier``
    configuration is set, so reading the predicate as an attribute would report
    ``external`` for a word that is only a condition.
    """
    start = _payload_start(tokens, open_index, close_index)
    if start is None:
        return ()
    regions = []
    index = start
    while index < close_index - 1:
        token = tokens[index]
        if token.kind is not Kind.IDENT or token.text not in VERUS_ATTRIBUTE_HEADS:
            index += 1
            continue
        end = _attribute_end(tokens, index, close_index)
        regions.append((index, end))
        index = end + 1
    return tuple(regions)


def _payload_start(
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
) -> int | None:
    """Return the index after a ``cfg_attr`` predicate's closing comma.

    The predicate is the first item and may itself contain commas inside a
    nested call such as ``all(…)``, so only a comma at the attribute's own
    bracket depth separates it from the payload.
    """
    depth = 0
    for index in range(open_index + 1, close_index):
        text = tokens[index].text
        if text in _OPENERS:
            depth += 1
        elif text in _CLOSERS:
            depth -= 1
        elif text == "," and depth == 0:
            return index + 1
    return None


def _attribute_end(
    tokens: tuple[Token, ...],
    head_index: int,
    close_index: int,
) -> int:
    """Return the last token of the attribute starting at ``head_index``.

    An attribute with arguments ends at its own closing bracket, so an
    unrelated attribute beside it in the payload is not swept into the region.
    """
    if head_index + 1 < close_index and tokens[head_index + 1].text == "(":
        return matching(tokens, head_index + 1, "(", ")")
    return head_index
