"""Decide whether a repetition hides a trust use from the lexical scan.

A ``macro_rules!`` expansion may repeat a metavariable, and the repetition can
supply the qualifier of a trust path, the item itself, or the name of an
invoked macro.  The arguments decide every one of those, so the scan refuses
the expansion rather than reporting the file clean.  The helpers walk outwards
through nested repetitions because each enclosing group adds a spelling the
lexer never resolves.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.structure import matching
from rust_prover_tools.trust_audit.syntax import Kind

if typ.TYPE_CHECKING:
    from rust_prover_tools.trust_audit.syntax import Token

__all__ = ("MACRO_DELIMITERS", "repetition_refusal")

# A path segment and its ``::``, as in ``vstd::prelude``.
_PATH_SEGMENT = 2
# A repetition separator may itself be several tokens, as ``>>`` is.
_MAX_SEPARATOR_TOKENS = 3

# The delimiters a macro invocation may open.
MACRO_DELIMITERS = frozenset({"(", "[", "{"})


def repetition_refusal(
    tokens: tuple[Token, ...],
    index: int,
    qualifiers: frozenset[str],
) -> str | None:
    """Refuse a metavariable whose enclosing repetition hides a trust use.

    A repetition can wrap the qualifier of a trust path —
    ``$($p)+::assume(..)`` — the item itself — ``kani::$($($f),+);+(..)`` —
    or an invocation — ``$($m)+!("hidden.rs")``.  Each produces a trust use
    the arguments decide, and the enclosing groups must be walked outwards
    because they may nest.
    """
    token = tokens[index]
    for extent in _repetition_extents(tokens, index):
        refusal = _boundary_refusal(tokens, token, extent, qualifiers)
        if refusal is not None:
            return refusal
    return None


def _boundary_refusal(
    tokens: tuple[Token, ...],
    token: Token,
    extent: tuple[int, int],
    qualifiers: frozenset[str],
) -> str | None:
    """Return the diagnostic for one repetition boundary, or ``None``."""
    before, after = extent
    following = tokens[after].text if after < len(tokens) else ""
    second = tokens[after + 1].text if after + 1 < len(tokens) else ""
    if following == "!" and second in MACRO_DELIMITERS:
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} invokes a macro through a repetition-wrapped "
            f"metavariable '${token.text}', so the audit cannot tell which "
            "macro runs"
        )
    if following == "::":
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a path through a repetition-wrapped "
            f"metavariable '${token.text}', so the audit cannot tell which "
            "crate or item it names"
        )
    if _follows_trust_qualifier(tokens, before, qualifiers):
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a trust item through a repetition-wrapped "
            f"metavariable '${token.text}', so the audit cannot tell which item "
            "is called"
        )
    return None


def _follows_trust_qualifier(
    tokens: tuple[Token, ...],
    before: int,
    qualifiers: frozenset[str],
) -> bool:
    """Report whether ``before`` is the ``::`` following a trust qualifier."""
    if before < 1 or tokens[before].text != "::":
        return False
    index = before - 1
    while (
        index >= _PATH_SEGMENT
        and tokens[index - 1].text == "::"
        and tokens[index - 2].kind is Kind.IDENT
    ):
        index -= _PATH_SEGMENT
    return tokens[index].text in qualifiers


def _repetition_extents(
    tokens: tuple[Token, ...],
    index: int,
) -> tuple[tuple[int, int], ...]:
    """Return every enclosing repetition's boundary, innermost first.

    Each pair is ``(before, after)``: the token in front of that group's ``$(``
    and the token after its repetition operator.  Every level is reported
    because an inner repetition can wrap a macro invocation while an outer one
    wraps the same expression, and only the inner boundary names the macro that
    runs.  A metavariable outside any repetition yields no pairs.
    """
    extents = []
    opener = _enclosing_repetition(tokens, index)
    while opener is not None:
        close = matching(tokens, opener, "(", ")")
        after = _after_repetition(tokens, close)
        if after is not None:
            extents.append((opener - 2, after))
        opener = _enclosing_repetition(tokens, opener)
    return tuple(extents)


def _enclosing_repetition(tokens: tuple[Token, ...], index: int) -> int | None:
    """Return the nearest ``$(`` group whose body holds the token at ``index``."""
    for opener in range(index - 2, 0, -1):
        if tokens[opener].text != "(" or tokens[opener - 1].text != "$":
            continue
        if matching(tokens, opener, "(", ")") > index:
            return opener
    return None


def _after_repetition(tokens: tuple[Token, ...], close: int) -> int | None:
    """Return the index after a repetition operator ending at ``close``.

    A repetition may name a separator between its group and the operator —
    ``$($f),+`` and ``$($f);+`` are both valid — so one intervening token is
    accepted as that separator.
    """
    following = close + 1
    for offset in range(0, _MAX_SEPARATOR_TOKENS + 1):
        operator = following + offset
        if operator < len(tokens) and tokens[operator].text in {"*", "+", "?"}:
            return operator + 1
    return None
