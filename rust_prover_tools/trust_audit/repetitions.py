"""Decide whether a repetition hides a trust use from the lexical scan.

A ``macro_rules!`` expansion may repeat a metavariable, and the repetition can
supply the qualifier of a trust path, the item itself, or the name of an
invoked macro.  The arguments decide every one of those, so the scan refuses
the expansion rather than reporting the file clean.  The helpers walk outwards
through nested repetitions because each enclosing group adds a spelling the
lexer never resolves.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.keywords import is_path_segment

if typ.TYPE_CHECKING:
    from rust_prover_tools.trust_audit.syntax import Token

__all__ = (
    "MACRO_DELIMITERS",
    "RepetitionIndex",
    "repetition_index",
    "repetition_refusal",
)

# A path segment and its ``::``, as in ``vstd::prelude``.
_PATH_SEGMENT = 2
# A repetition separator may itself be several tokens, as ``>>`` is.
_MAX_SEPARATOR_TOKENS = 3

# The delimiters a macro invocation may open.
MACRO_DELIMITERS = frozenset({"(", "[", "{"})


@dc.dataclass(frozen=True, slots=True)
class RepetitionIndex:
    """Every token's enclosing repetitions, built in one pass.

    Each token records the innermost ``$( … )`` holding it, and each repetition
    opener records its parent.  A metavariable's extents are then read by
    walking outwards, so a transcriber with many ordinary metavariables does
    not rescan the file for each one.
    """

    _innermost: tuple[int | None, ...]
    _parents: tuple[int | None, ...]
    _extents: tuple[tuple[int, int] | None, ...]

    def extents(self, index: int) -> tuple[tuple[int, int], ...]:
        """Return the extents enclosing ``index``, innermost first."""
        if index < 0 or index >= len(self._innermost):
            return ()
        found: list[tuple[int, int]] = []
        opener = self._innermost[index]
        while opener is not None:
            extent = self._extents[opener]
            if extent is not None:
                found.append(extent)
            opener = self._parents[opener]
        return tuple(found)


def repetition_index(tokens: tuple[Token, ...]) -> RepetitionIndex:
    """Index every repetition group and the token ranges they enclose."""
    innermost: list[int | None] = [None] * len(tokens)
    parents: list[int | None] = [None] * len(tokens)
    extents: list[tuple[int, int] | None] = [None] * len(tokens)
    openers: list[int] = []
    repetitions: list[int] = []
    for index, token in enumerate(tokens):
        if token.text == "(":
            openers.append(index)
            innermost[index] = repetitions[-1] if repetitions else None
            if index >= 1 and tokens[index - 1].text == "$":
                parents[index] = repetitions[-1] if repetitions else None
                repetitions.append(index)
                innermost[index] = index
        elif token.text == ")" and openers:
            opener = openers.pop()
            if opener >= 1 and tokens[opener - 1].text == "$":
                if repetitions and repetitions[-1] == opener:
                    repetitions.pop()
                after = _after_repetition(tokens, index)
                if after is not None:
                    extents[opener] = (opener - 2, after)
            innermost[index] = repetitions[-1] if repetitions else None
        else:
            innermost[index] = repetitions[-1] if repetitions else None
    return RepetitionIndex(tuple(innermost), tuple(parents), tuple(extents))


def repetition_refusal(
    tokens: tuple[Token, ...],
    index: int,
    qualifiers: frozenset[str],
    *,
    repetitions: RepetitionIndex | None = None,
) -> str | None:
    """Refuse a metavariable whose enclosing repetition hides a trust use.

    A repetition can wrap the qualifier of a trust path —
    ``$($p)+::assume(..)`` — the item itself — ``kani::$($($f),+);+(..)`` —
    or an invocation — ``$($m)+!("hidden.rs")``.  Each produces a trust use
    the arguments decide, and the enclosing groups must be walked outwards
    because they may nest.  A caller that scans many metavariables supplies
    one index for the whole token stream; otherwise it is built here.
    """
    token = tokens[index]
    lookup = repetitions if repetitions is not None else repetition_index(tokens)
    for extent in lookup.extents(index):
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
    """Report whether ``before`` is the ``::`` following a trust qualifier.

    The walk stops at a keyword such as ``return``: in ``return ::kani::$($f)+``
    the leading ``::`` starts an absolute path, and the keyword in front of it
    is not the path's root.  Reading it as a segment would make the same item
    refuse or pass depending on the expression the repetition is written in.
    """
    if before < 1 or tokens[before].text != "::":
        return False
    index = before - 1
    while (
        index >= _PATH_SEGMENT
        and tokens[index - 1].text == "::"
        and is_path_segment(tokens[index - 2])
    ):
        index -= _PATH_SEGMENT
    return tokens[index].text in qualifiers


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
