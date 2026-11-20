"""Locate bracketed groups in a lexed Rust source.

Both halves of the audit walk brackets. The matcher needs an attribute's span
to decide whether it carries Verus vocabulary, and it needs a ``verus!`` body's
span to bound that region; the module walker needs the span to read a
``#[path = "…"]`` value. The two searches must agree about which ``]`` closes
which ``[``, because an attribute read one token short silently moves the
region it names. Keeping the bracket arithmetic in one module means neither
caller can drift from the other.
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.syntax import (
    Kind,
    Token,
    TrustAuditError,
    literal_body,
)

__all__ = (
    "attribute_spans",
    "matching",
    "opening_bracket",
    "opening_paren",
    "path_value",
    "previous_closer",
    "skip_visibility",
)

# The tokens between ``#[`` and a path attribute's value.
_PATH_HEAD = ("path", "=")


def matching(
    tokens: tuple[Token, ...],
    open_index: int,
    opener: str,
    closer: str,
) -> int:
    """Return the index of the token closing the group opened at ``open_index``.

    Raises
    ------
    TrustAuditError
        If the group never closes.

    Examples
    --------
    For tokens ``( verus! { x } )``, ``matching`` on the ``{`` reports the
    ``}``.
    """
    depth = 0
    for index in range(open_index, len(tokens)):
        if tokens[index].text == opener:
            depth += 1
        elif tokens[index].text == closer:
            depth -= 1
            if depth == 0:
                return index
    msg = (
        f"unclosed {opener!r} opened at line {tokens[open_index].line}, "
        f"column {tokens[open_index].column}"
    )
    raise TrustAuditError(msg)


def attribute_spans(tokens: tuple[Token, ...]) -> tuple[tuple[int, int], ...]:
    """Return ``(open, close)`` bracket indices for every attribute.

    Both forms are ordinary attributes for this purpose: ``#[cfg_attr(…)]`` and
    ``#![cfg_attr(…)]`` carry a Verus attribute equally well. Reading a span
    from the wrong bracket would make every attribute one token short, so the
    outer and inner forms are distinguished explicitly.
    """
    spans = []
    for index, token in enumerate(tokens):
        if token.text != "#":
            continue
        inner = index + 1 < len(tokens) and tokens[index + 1].text == "!"
        opening = index + 2 if inner else index + 1
        if opening < len(tokens) and tokens[opening].text == "[":
            spans.append((opening, matching(tokens, opening, "[", "]")))
    return tuple(spans)


def previous_closer(tokens: tuple[Token, ...], opener: int) -> int:
    """Return the index of the ``]`` closing the attribute before ``opener``.

    Searching backwards rather than stepping a fixed distance keeps the walk
    correct for both ``#[…]`` and ``#![…]``: the two forms differ by one token
    at the front, and an offset calibrated for one is off by one for the other.
    ``-1`` means no attribute precedes ``opener``.
    """
    for index in range(opener - 1, -1, -1):
        if tokens[index].text == "]":
            return index
        if tokens[index].text not in {"#", "!"}:
            return -1
    return -1


def skip_visibility(tokens: tuple[Token, ...], index: int) -> int:
    """Step backwards over ``pub`` or ``pub(…)`` and return the index before it.

    ``pub(in path)`` and ``pub(crate)`` are handled by skipping the matching
    parentheses, so the four visibility forms need no separate cases.
    """
    if index >= 0 and tokens[index].text == "pub":
        return index - 1
    if index < 0 or tokens[index].text != ")":
        return index
    opener = opening_paren(tokens, index)
    if opener is None or opener < 1:
        return index
    return opener - 2 if tokens[opener - 1].text == "pub" else index


def opening_paren(tokens: tuple[Token, ...], close_index: int) -> int | None:
    """Return the index of the ``(`` matching the ``)`` at ``close_index``."""
    depth = 0
    for index in range(close_index, -1, -1):
        if tokens[index].text == ")":
            depth += 1
        elif tokens[index].text == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def opening_bracket(tokens: tuple[Token, ...], close_index: int) -> int | None:
    """Return the index of the ``[`` matching the ``]`` at ``close_index``."""
    depth = 0
    for index in range(close_index, -1, -1):
        if tokens[index].text == "]":
            depth += 1
        elif tokens[index].text == "[":
            depth -= 1
            if depth == 0:
                return index
    return None


def path_value(
    tokens: tuple[Token, ...], open_index: int, close_index: int
) -> str | None:
    """Return the value of a ``#[path = "…"]`` span, if it is one."""
    value_index = open_index + 1 + len(_PATH_HEAD)
    if value_index >= close_index:
        return None
    head = tuple(token.text for token in tokens[open_index + 1 : value_index])
    if head != _PATH_HEAD:
        return None
    literal = tokens[value_index]
    return literal_body(literal.text)[1] if literal.kind is Kind.LITERAL else None
