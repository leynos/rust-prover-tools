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

if typ.TYPE_CHECKING:
    from rust_prover_tools.trust_audit.syntax import Token

__all__ = ("MACRO_DELIMITERS", "repetition_refusal")

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
    extent = _repetition_extent(tokens, index)
    if extent is None:
        return None
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
    return tokens[before - 1].text in qualifiers


def _opens_repetition(tokens: tuple[Token, ...], opener: int) -> bool:
    """Report whether ``opener`` is a ``(`` introduced by a repetition ``$``."""
    if opener < 1:
        return False
    return tokens[opener - 1].text == "$"


def _repetition_extent(
    tokens: tuple[Token, ...],
    index: int,
) -> tuple[int, int] | None:
    """Return the tokens before and after a metavariable's outermost repetition.

    The walk starts at the innermost ``$( … )`` holding the metavariable and
    continues outwards while the group and its operator are themselves the body
    of another repetition.  ``before`` is the token in front of the outermost
    ``$(``; ``after`` is the token after its repetition operator.  A ``None``
    result means the metavariable is not inside a repetition the scan can
    delimit.
    """
    opener = _enclosing_repetition(tokens, index)
    if opener is None:
        return None
    while True:
        close = matching(tokens, opener, "(", ")")
        after = _after_repetition(tokens, close)
        if after is None:
            return None
        if after < len(tokens) and tokens[after].text == ")":
            outer = _enclosing_group_opener(tokens, after)
            if outer is not None and _opens_repetition(tokens, outer):
                opener = outer
                continue
        return opener - 2, after


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
    if following < len(tokens) and tokens[following].text in {"*", "+", "?"}:
        return following + 1
    if following + 1 < len(tokens) and tokens[following + 1].text in {"*", "+", "?"}:
        return following + 2
    return None


def _enclosing_group_opener(tokens: tuple[Token, ...], close: int) -> int | None:
    """Return the ``(`` matching the ``)`` at ``close``."""
    depth = 0
    for index in range(close, -1, -1):
        if tokens[index].text == ")":
            depth += 1
        elif tokens[index].text == "(":
            depth -= 1
            if depth == 0:
                return index
    return None
