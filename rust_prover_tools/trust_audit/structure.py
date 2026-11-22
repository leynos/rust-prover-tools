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

import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.literals import decoded_literal_body
from rust_prover_tools.trust_audit.syntax import Kind, Token

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "attribute_spans",
    "matching",
    "opening_bracket",
    "opening_paren",
    "path_value",
    "previous_closer",
    "skip_visibility",
    "stringify_arguments",
    "texts",
)

# The tokens between ``#[`` and a path attribute's value.
_PATH_HEAD = ("path", "=")
# The bracket pairs an attribute or use tree may nest.
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}
_OPENERS = frozenset(_DELIMITER_PAIRS)
_CLOSERS = frozenset(_DELIMITER_PAIRS.values())


def matching(
    tokens: tuple[Token, ...],
    open_index: int,
    opener: str,
    closer: str,
) -> int:
    """Return the index of the token closing the group opened at ``open_index``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    open_index : int
        Index of the opening delimiter in ``tokens``.
    opener : str
        The opening delimiter's text, such as ``"{"``.
    closer : str
        The matching closing delimiter's text, such as ``"}"``.

    Returns
    -------
    int
        The index of the closing token.

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


def texts(tokens: tuple[Token, ...], start: int, count: int) -> tuple[str, ...]:
    """Return up to ``count`` token texts from ``start``, clipped at the end.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Index of the first token to read.
    count : int
        The maximum number of texts to return.

    Returns
    -------
    tuple[str, ...]
        The texts found, fewer than ``count`` when the source ends first.

    """
    return tuple(token.text for token in tokens[start : start + count])


def attribute_spans(tokens: tuple[Token, ...]) -> tuple[tuple[int, int], ...]:
    """Return ``(open, close)`` bracket indices for every attribute.

    Both forms are ordinary attributes for this purpose: ``#[cfg_attr(…)]`` and
    ``#![cfg_attr(…)]`` carry a Verus attribute equally well. Reading a span
    from the wrong bracket would make every attribute one token short, so the
    outer and inner forms are distinguished explicitly.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.

    Returns
    -------
    tuple[tuple[int, int], ...]
        One ``(open, close)`` pair of bracket indices per attribute, in source
        order.

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

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    opener : int
        Token index of an attribute's opening ``[``.

    Returns
    -------
    int
        Index of the ``]`` closing the attribute immediately before
        ``opener``, or ``-1`` when no attribute precedes it.

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

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    index : int
        Token index to step backwards from.

    Returns
    -------
    int
        The index before the visibility modifier, or ``index`` unchanged when
        the token there is not one.

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
    """Return the index of the ``(`` matching the ``)`` at ``close_index``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    close_index : int
        Token index of the ``)`` to match.

    Returns
    -------
    int | None
        The matching ``(`` index, or ``None`` when none encloses it.

    """
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
    """Return the index of the ``[`` matching the ``]`` at ``close_index``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    close_index : int
        Token index of the ``]`` to match.

    Returns
    -------
    int | None
        The matching ``[`` index, or ``None`` when none encloses it.

    """
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
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
    *,
    refuse_unresolved: bool = False,
) -> str | None:
    r"""Return the value of a ``#[path = "…"]`` span, if it is one.

    The value is Rust's reading of the literal, not its source spelling, so an
    escape such as ``"\\x63hild.rs"`` names ``child.rs`` exactly as the
    compiler resolves it.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    open_index : int
        Token index of the attribute's ``[``.
    close_index : int
        Token index of that attribute's ``]``.
    refuse_unresolved : bool
        Raise when the span is a path attribute whose value is not a literal.
        A caller that resolves module files sets this, because a metavariable
        or other expression would otherwise read as "no path" and send the walk
        to the default file instead of the one the expansion compiles.

    Returns
    -------
    str | None
        The decoded value, or ``None`` when the span is not a
        ``#[path = "…"]`` attribute.

    Raises
    ------
    TrustAuditError
        If the span is a path attribute with an unresolvable value and
        ``refuse_unresolved`` is set.

    """
    value_index = open_index + 1 + len(_PATH_HEAD)
    if value_index >= close_index:
        return None
    head = tuple(token.text for token in tokens[open_index + 1 : value_index])
    if head != _PATH_HEAD:
        return None
    literal = tokens[value_index]
    if literal.kind is not Kind.LITERAL:
        if not refuse_unresolved:
            return None
        msg = (
            f"the #[path] attribute at line {literal.line}, column "
            f"{literal.column} has a value the audit cannot resolve"
        )
        raise TrustAuditError(msg)
    return decoded_literal_body(literal.text)[1]


# The delimiters a macro invocation's body may open.
_MACRO_DELIMITERS = {"(": ")", "[": "]", "{": "}"}


def _is_builtin_stringify(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``stringify`` at ``index`` names the builtin macro.

    A qualified path other than ``std::stringify`` or ``core::stringify`` can
    name a user macro that emits its argument, so it earns no exemption.
    """
    if index < 1 or tokens[index - 1].text != "::":
        return True
    qualifier = index - 2
    if qualifier < 0:
        return False
    return tokens[qualifier].text in {"std", "core"}


def _imports_stringify(tokens: tuple[Token, ...]) -> bool:
    """Report whether a ``use`` statement binds the name ``stringify``."""
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or token.raw:
            continue
        if token.text != "use":
            continue
        for cursor in range(index + 1, len(tokens)):
            if tokens[cursor].text == ";":
                break
            if tokens[cursor].text == "stringify":
                return True
    return False


def stringify_arguments(
    tokens: tuple[Token, ...],
    *,
    defined: cabc.Container[str] = frozenset(),
) -> frozenset[int]:
    """Return the token indices inside every ``stringify!`` argument group.

    ``stringify!`` returns its argument's spelling and compiles none of it, so
    a declaration or invocation written there names nothing.  The argument is
    skipped by its delimiters rather than by matching each line.  A file that
    defines its own ``stringify`` macro compiles the argument, so there is no
    exemption for it.
    """
    if "stringify" in defined or _imports_stringify(tokens):
        return frozenset()
    skipped: set[int] = set()
    for index, token in enumerate(tokens[:-2]):
        if token.kind is not Kind.IDENT or token.text != "stringify":
            continue
        if not _is_builtin_stringify(tokens, index):
            continue
        if tokens[index + 1].text != "!":
            continue
        opening = index + 2
        closer = _MACRO_DELIMITERS.get(tokens[opening].text)
        if closer is None:
            continue
        closing = matching(tokens, opening, tokens[opening].text, closer)
        skipped.update(range(opening, closing + 1))
    return frozenset(skipped)
