"""Locate the token regions that carry Verus' own vocabulary.

A Verus word is a request for trust only where Verus gives it meaning: inside a
``verus!`` body, or in an attribute such as ``verifier``. This module answers
"which token ranges are those" once per file, so the matcher can ask a range
question rather than re-deriving the boundaries for every word it classifies.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.conditions import attribute_items, payload_start
from rust_prover_tools.trust_audit.crate_bindings import alias_bindings
from rust_prover_tools.trust_audit.structure import attribute_spans, matching
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_bindings import member_bindings
from rust_prover_tools.trust_audit.vocabulary import (
    VERUS_ATTRIBUTE_HEADS,
    VERUS_ROOTS,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("verus_regions",)

# The macro accepts the same items in any of Rust's three delimiter pairs, so
# all three are read.
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}


def verus_regions(tokens: tuple[Token, ...]) -> tuple[tuple[int, int], ...]:
    """Return the token ranges whose identifiers carry Verus' own vocabulary.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.

    Returns
    -------
    tuple[tuple[int, int], ...]
        One ``(start, end)`` pair per region, as inclusive token indices.  A
        ``verus!`` body ends at its closing delimiter and an attribute at its
        closing bracket or last token, so an end may name a delimiter rather
        than an identifier; a caller's inclusive range test reads that
        harmlessly.

    Notes
    -----
    Two forms create a region: a ``verus!`` body, and a Verus attribute
    anywhere in an attribute list, including one nested in a ``cfg_attr``.  A
    ``cfg_attr`` predicate is not a region: it asks whether a configuration is
    set rather than applying an attribute.

    Examples
    --------
    ``verus! { proof fn f() {} }`` is one region, and
    ``#[verifier::external_body]`` is another.
    """
    regions = list(_verus_bodies(tokens))
    for open_index, close_index in attribute_spans(tokens):
        regions.extend(_verus_attribute_regions(tokens, open_index, close_index))
    return tuple(regions)


def _verus_bodies(tokens: tuple[Token, ...]) -> cabc.Iterator[tuple[int, int]]:
    """Locate each ``verus!`` body, tolerating nesting and any delimiter.

    The macro accepts the same items in braces, parentheses or brackets, so
    reading only ``verus! { … }`` would leave the body of ``verus!( … )``
    ordinary Rust and lose every Verus word in it.  A local alias is the same
    macro: ``use vstd::prelude::{verus as v, *};`` makes ``v! { … }`` a Verus
    body, and treating it as an unknown macro would leave its words unread.
    """
    names = _verus_macro_names(tokens)
    for index, token in enumerate(tokens[:-2]):
        if token.text not in names or tokens[index + 1].text != "!":
            continue
        opening = index + 2
        closer = _DELIMITER_PAIRS.get(tokens[opening].text)
        if closer is None:
            continue
        yield opening + 1, matching(tokens, opening, tokens[opening].text, closer)


def _verus_macro_names(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return ``verus`` and every local name the file binds it to.

    A crate may be renamed before its macro is imported —
    ``use vstd as local_vstd; use local_vstd::prelude::verus as local_verus;``
    — so the roots are expanded with their aliases before the member search.
    Reading only the literal roots would leave such a body unread.
    """
    roots = set(VERUS_ROOTS)
    for root in VERUS_ROOTS:
        roots.update(alias_bindings(tokens, root))
    names = {"verus"}
    names.update(
        alias
        for item, alias in member_bindings(tokens, frozenset(roots))
        if item == "verus"
    )
    return frozenset(names)


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

    Each payload item is a whole attribute, so an unrelated attribute's
    arguments are skipped rather than swept into a region.  A literal such as
    ``doc = stringify!(verifier::external_body)`` is documentation, not an
    applied Verus attribute, and is not read.  A nested ``cfg_attr`` is
    recursed into through its own payload.
    """
    start = payload_start(tokens, open_index, close_index)
    if start is None:
        return ()
    regions = []
    for item_start, item_end in attribute_items(tokens, start, close_index):
        head = tokens[item_start]
        if head.kind is not Kind.IDENT:
            continue
        end = _attribute_end(tokens, item_start, item_end)
        if head.text in VERUS_ATTRIBUTE_HEADS:
            regions.append((item_start, end))
        elif _is_nested_cfg_attr(tokens, item_start, item_end):
            inner_close = matching(tokens, item_start + 1, "(", ")")
            regions.extend(_cfg_attr_regions(tokens, item_start + 1, inner_close))
    return tuple(regions)


def _is_nested_cfg_attr(
    tokens: tuple[Token, ...],
    item_start: int,
    item_end: int,
) -> bool:
    """Report whether one payload item is a nested ``cfg_attr(…)`` call."""
    if tokens[item_start].text != "cfg_attr":
        return False
    return item_start + 1 < item_end and tokens[item_start + 1].text == "("


def _attribute_end(
    tokens: tuple[Token, ...],
    head_index: int,
    close_index: int,
) -> int:
    """Return the last token of the attribute starting at ``head_index``.

    An attribute path may be qualified — ``verifier::external_body`` is the
    ``external_body`` attribute of the ``verifier`` namespace — and every
    segment is part of the attribute, so the path is followed before the
    argument group is looked for. An attribute with arguments ends at its own
    closing bracket, so an unrelated attribute beside it in the payload is not
    swept into the region.
    """
    end = head_index
    while (
        end + 2 < close_index
        and tokens[end + 1].text == "::"
        and tokens[end + 2].kind is Kind.IDENT
    ):
        end += 2
    if end + 1 < close_index and tokens[end + 1].text == "(":
        return matching(tokens, end + 1, "(", ")")
    return end
