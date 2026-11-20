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

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.literals import decoded_literal_body
from rust_prover_tools.trust_audit.syntax import Kind, Token

__all__ = (
    "attribute_spans",
    "conditional_path",
    "matching",
    "opening_bracket",
    "opening_paren",
    "path_value",
    "payload_start",
    "previous_closer",
    "skip_visibility",
    "texts",
)

# The tokens between ``#[`` and a path attribute's value.
_PATH_HEAD = ("path", "=")
# The attribute that applies another attribute under a configuration predicate.
_CFG_ATTR = "cfg_attr"
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
    """Return up to ``count`` token texts from ``start``, clipped at the end."""
    return tuple(token.text for token in tokens[start : start + count])


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
    r"""Return the value of a ``#[path = "…"]`` span, if it is one.

    The value is Rust's reading of the literal, not its source spelling, so an
    escape such as ``"\\x63hild.rs"`` names ``child.rs`` exactly as the
    compiler resolves it.
    """
    value_index = open_index + 1 + len(_PATH_HEAD)
    if value_index >= close_index:
        return None
    head = tuple(token.text for token in tokens[open_index + 1 : value_index])
    if head != _PATH_HEAD:
        return None
    literal = tokens[value_index]
    if literal.kind is not Kind.LITERAL:
        return None
    return decoded_literal_body(literal.text)[1]


def payload_start(
    tokens: tuple[Token, ...], open_index: int, close_index: int
) -> int | None:
    """Return the index after a ``cfg_attr`` predicate's closing comma.

    The predicate is the first item and may itself contain commas inside a
    nested call such as ``all(…)``, so only a comma at the attribute's own
    bracket depth separates it from the payload. ``None`` means the attribute
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


def conditional_path(
    tokens: tuple[Token, ...], open_index: int, close_index: int
) -> bool:
    """Report whether a ``cfg_attr`` span can select a ``#[path]`` target.

    ``#[cfg_attr(unix, path = "unix_impl.rs")] mod platform;`` compiles one of
    two different files depending on the configuration. The audit reads the
    repository rather than one configuration, so a declaration like this is
    refused instead of being resolved to whichever file happens to exist.

    Only the payload can select a target. The configuration predicate is the
    first item, so a predicate that happens to spell ``path = "…"`` — a custom
    configuration named ``path`` with a value — is not a path attribute.
    """
    head_index = open_index + 1
    if head_index >= close_index or tokens[head_index].text != _CFG_ATTR:
        return False
    start = payload_start(tokens, head_index + 1, close_index)
    if start is None:
        return False
    return any(
        tuple(token.text for token in tokens[index : index + len(_PATH_HEAD)])
        == _PATH_HEAD
        for index in range(start, close_index)
    )
