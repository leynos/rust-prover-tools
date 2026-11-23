"""Read the attributes a ``cfg_attr`` applies under a configuration predicate.

``#[cfg_attr(unix, path = "unix_impl.rs")] mod platform;`` compiles one of two
different files depending on the configuration.  The audit reads one tree, so
a declaration like that is refused rather than resolved to whichever file
happens to exist.

Reading it correctly means reading the payload as a list of attributes.  The
first argument is a predicate, not an applied attribute, and a nested
``cfg_attr`` keeps its own predicate in the same position.  A predicate may
spell ``path = "…"`` as a custom configuration, so a scan for the word
``path`` anywhere in the attribute would refuse a declaration whose file no
configuration can change.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.structure import matching

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.trust_audit.syntax import Token

__all__ = (
    "attribute_disables",
    "attribute_items",
    "conditional_path",
    "payload_start",
)

#: The tokens between ``#[`` and a path attribute's value.
_PATH_HEAD = ("path", "=")
#: The attribute that applies another attribute under a configuration predicate.
_CFG_ATTR = "cfg_attr"
#: The bracket pairs an attribute may nest.
_OPENERS = frozenset({"(", "[", "{"})
_CLOSERS = frozenset({")", "]", "}"})


def payload_start(
    tokens: tuple[Token, ...], open_index: int, close_index: int
) -> int | None:
    """Return the index after a ``cfg_attr`` predicate's closing comma.

    The predicate is the first item and may itself contain commas inside a
    nested call such as ``all(…)``, so only a comma at the attribute's own
    bracket depth separates it from the payload.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    open_index : int
        Token index of the ``cfg_attr`` call's ``(``.
    close_index : int
        Token index of its matching ``)``.

    Returns
    -------
    int | None
        Index of the first payload token, or ``None`` when the attribute
        carries no payload and so applies no attribute at all.

    """
    if open_index >= close_index or tokens[open_index].text != "(":
        return None
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


def attribute_disables(
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
) -> bool:
    """Report whether a ``cfg`` span is disabled in every configuration.

    ``#[cfg(any())]`` has an empty ``any``, which is false whatever is set, so
    the item it decorates is never compiled.  The audit reads one tree, and a
    module that tree cannot contain must not be demanded as a file.  Only that
    one provably false shape is recognised; every other predicate may be
    enabled by some configuration, and is left to the ordinary conservative
    handling.
    """
    head_index = open_index + 1
    if head_index >= close_index or tokens[head_index].text != "cfg":
        return False
    payload = head_index + 1
    if payload >= close_index or tokens[payload].text != "(":
        return False
    closing = matching(tokens, payload, "(", ")")
    if closing <= payload + 1 or tokens[payload + 1].text != "any":
        return False
    inner = payload + 2
    return (
        inner < closing
        and tokens[inner].text == "("
        and matching(tokens, inner, "(", ")") == inner + 1
        and inner + 2 == closing
    )


def conditional_path(
    tokens: tuple[Token, ...], open_index: int, close_index: int
) -> bool:
    """Report whether a ``cfg_attr`` span can select a ``#[path]`` target.

    Only the payload can select a target.  The configuration predicate is the
    first item, so a predicate that happens to spell ``path = "…"`` — a custom
    configuration named ``path`` with a value — is not a path attribute, and a
    nested ``cfg_attr`` is recursed into through its own payload.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    open_index : int
        Token index of the attribute's ``[``.
    close_index : int
        Token index of that attribute's ``]``.

    Returns
    -------
    bool
        Whether the attribute is a ``cfg_attr`` whose payload carries
        ``path = …``.

    """
    head_index = open_index + 1
    if head_index >= close_index or tokens[head_index].text != _CFG_ATTR:
        return False
    start = payload_start(tokens, head_index + 1, close_index)
    if start is None:
        return False
    return _payload_selects_path(tokens, start, close_index)


def _payload_selects_path(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether an attribute payload applies a ``path`` attribute."""
    for item_start, item_end in attribute_items(tokens, start, end):
        if _is_path_item(tokens, item_start, item_end):
            return True
        if _is_nested_cfg_attr(tokens, item_start, item_end):
            inner_close = matching(tokens, item_start + 1, "(", ")")
            inner_start = payload_start(tokens, item_start + 1, inner_close)
            if inner_start is not None and _payload_selects_path(
                tokens,
                inner_start,
                inner_close,
            ):
                return True
    return False


def _is_path_item(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether one payload item is ``path = …``.

    Any value counts, not only a literal.  A ``path`` whose value is not a
    literal is still a path the configuration can select, and the caller
    refuses it rather than letting the declaration fall back to its default
    file.
    """
    if tuple(token.text for token in tokens[start : start + len(_PATH_HEAD)]) != (
        _PATH_HEAD
    ):
        return False
    return start + len(_PATH_HEAD) < end


def _is_nested_cfg_attr(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether one payload item is a nested ``cfg_attr(…)`` call."""
    return (
        start + 1 < end
        and tokens[start].text == _CFG_ATTR
        and tokens[start + 1].text == "("
    )


def attribute_items(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[int, int]]:
    """Yield the top-level comma-separated items of an attribute payload.

    Each item's end is the comma that separates it, or ``end`` for the last
    one.  Commas inside a nested group belong to that group, so an item such
    as ``cfg_attr(pred, allow(a, b))`` stays whole.
    """
    item_start = start
    depth = 0
    for index in range(start, end):
        text = tokens[index].text
        if text in _OPENERS:
            depth += 1
        elif text in _CLOSERS:
            depth -= 1
        elif text == "," and depth == 0:
            yield item_start, index
            item_start = index + 1
    if item_start < end:
        yield item_start, end
