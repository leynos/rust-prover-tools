"""Read a GitHub Actions `if:` condition as a conjunction of whole terms.

A guard is asserted term by term rather than as a substring. A substring
check on `github.ref == 'refs/heads/main'` accepts
`... && github.ref == 'refs/heads/main' || github.event_name == ...`,
which makes every term optional, so a condition carrying an unquoted
`||` is refused outright and the rest is split on `&&`. Both are read
only at parenthesis depth zero: `!(a && b)` is one term, not two, and
`a && (b || c)` is a conjunction whose second term narrows the first.
"""

from __future__ import annotations

import re
import typing as typ

#: The expression wrapper GitHub accepts around a whole `if:` condition.
_WRAPPED: typ.Final[re.Pattern[str]] = re.compile(
    r"^\$\{\{(?P<body>.*)\}\}$", re.DOTALL
)


class ConditionError(ValueError):
    """Raised when a condition cannot be read as a conjunction."""


def _unwrap(condition: str) -> str:
    """Return the condition without an enclosing `${{ }}`."""
    stripped = condition.strip()
    match = _WRAPPED.match(stripped)
    return match.group("body").strip() if match else stripped


def _levels(text: str) -> list[int | None]:
    """Return each character's parenthesis depth, or None inside a literal.

    GitHub expressions quote strings with single quotes and escape one by
    doubling it, so toggling on every quote tracks the state correctly.

    Raises
    ------
    ConditionError
        If a quote or a parenthesis is left unbalanced.

    """
    levels: list[int | None] = []
    depth = 0
    quoted = False
    for char in text:
        quoted = quoted != (char == "'")
        depth += 0 if quoted else {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            break
        levels.append(None if quoted or char == "'" else depth)
    if quoted or depth:
        message = f"unbalanced quotes or parentheses in {text!r}"
        raise ConditionError(message)
    return levels


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on a separator wherever it falls outside literals and groups."""
    levels = _levels(text)
    cuts = [
        index
        for index, level in enumerate(levels)
        if level == 0 and text.startswith(separator, index)
    ]
    starts = [0, *(cut + len(separator) for cut in cuts)]
    ends = [*cuts, len(text)]
    return [text[start:end] for start, end in zip(starts, ends, strict=True)]


def _normalise(term: str) -> str:
    """Collapse runs of whitespace so spacing cannot defeat a comparison."""
    return " ".join(term.split())


def conjuncts(condition: object) -> list[str]:
    """Return the whole terms of an `&&` conjunction.

    Raises
    ------
    ConditionError
        If the condition is not text, is unbalanced, or carries an
        ungrouped, unquoted `||`, which would make every term optional.

    Examples
    --------
    >>> conjuncts("${{ a == 'x&&y' &&  b }}")
    ["a == 'x&&y'", 'b']

    """
    if not isinstance(condition, str):
        message = f"condition {condition!r} is not an expression"
        raise ConditionError(message)
    body = _unwrap(condition)
    if len(_split_top_level(body, "||")) > 1:
        message = f"condition {condition!r} carries an unquoted `||`"
        raise ConditionError(message)
    return [_normalise(term) for term in _split_top_level(body, "&&")]


def missing_terms(condition: object, required: frozenset[str]) -> list[str]:
    """Return the required terms a condition does not carry whole.

    Extra terms are permitted, since they only narrow when a step runs.

    Raises
    ------
    ConditionError
        If the condition cannot be read as a conjunction.

    """
    present = set(conjuncts(condition))
    return sorted(term for term in required if term not in present)
