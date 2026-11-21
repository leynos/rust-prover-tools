"""Collect the ``macro_rules!`` definitions and invocations in one source.

A ``macro_rules!`` rule has a matcher and an expansion.  A matcher is syntax
rather than code, so a ``mod sample;`` spelled there compiles nothing; an
expansion is code the crate compiles, but only where the macro is invoked.  The
invocation site is the directory rustc resolves the expansion's declarations
from, so the caller needs both halves of that fact.

A name defined more than once is refused.  Rust permits a later definition to
shadow an earlier one, and the lexical scan cannot tell which definition a
given invocation expands; returning either module tree would be a guess, and a
wrong guess can scan a clean file in place of the compiled one.

The collector is pure: it reads the token stream and reports spans and names.
`rust_prover_tools.trust_audit.modules` turns the result into module references.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.structure import matching, texts
from rust_prover_tools.trust_audit.syntax import Kind, Token

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("MacroRules", "macro_rules")

_MACRO_HEAD = ("macro_rules", "!")
_RULE_ARROW = "=>"
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}


@dc.dataclass(frozen=True, slots=True)
class MacroRules:
    """Where a ``macro_rules!`` definition's tokens are and who invokes it.

    ``owners`` maps every token inside an expansion to the macro that owns it;
    ``expansions_by_name`` is the same fact grouped for a traversal.
    ``invocations`` includes calls written inside another expansion, which is
    what lets the caller follow a macro that invokes a second one.
    """

    patterns: frozenset[int]
    expansions: frozenset[int]
    heads: dict[str, int]
    expansions_by_name: dict[str, tuple[int, ...]]
    owners: dict[int, str]
    invocations: dict[str, tuple[int, ...]]
    definitions: frozenset[int]


def macro_rules(tokens: tuple[Token, ...]) -> MacroRules:
    """Collect every ``macro_rules!`` definition and invocation in the file.

    A rule is a pattern followed by ``=>`` and a body, so the group before each
    arrow is the pattern and the group after it is the expansion.  Only the
    pattern is syntax; an expansion that emits ``mod x;`` is code the crate
    compiles, at each site the macro is invoked.

    Raises
    ------
    TrustAuditError
        If two definitions share a name, because the scan cannot tell which one
        an invocation expands.
    """
    heads, definitions = _definition_bounds(tokens)
    patterns, expansions, owners = _rule_tokens(tokens, heads)
    return MacroRules(
        frozenset(patterns),
        frozenset(expansions),
        heads,
        _expansions_by_name(owners),
        owners,
        _macro_invocations(tokens, patterns),
        frozenset(definitions),
    )


def _expansions_by_name(owners: dict[int, str]) -> dict[str, tuple[int, ...]]:
    """Group an expansion's token indices by the macro that owns them."""
    grouped: dict[str, list[int]] = {}
    for index, name in owners.items():
        grouped.setdefault(name, []).append(index)
    return {name: tuple(indices) for name, indices in grouped.items()}


@dc.dataclass(slots=True)
class _Definitions:
    """The definitions found so far, keyed by their name."""

    heads: dict[str, int] = dc.field(default_factory=dict)
    definitions: set[int] = dc.field(default_factory=set)


def _definition_bounds(
    tokens: tuple[Token, ...],
) -> tuple[dict[str, int], set[int]]:
    """Return each macro's head index and every token inside a definition."""
    found = _Definitions()
    for index in range(len(tokens) - len(_MACRO_HEAD)):
        spans = _definition_spans(tokens, index)
        if spans is not None:
            _record_definition(tokens, found, index, spans)
    return found.heads, found.definitions


def _record_definition(
    tokens: tuple[Token, ...],
    found: _Definitions,
    index: int,
    spans: tuple[str, int, int],
) -> None:
    """Record one definition, refusing a name a second definition reuses."""
    name, _, closing = spans
    if name in found.heads:
        token = tokens[index + len(_MACRO_HEAD)]
        msg = (
            f"macro_rules! defines {name!r} more than once, at line "
            f"{token.line}, column {token.column}; the audit cannot tell which "
            "definition an invocation expands"
        )
        raise TrustAuditError(msg)
    found.heads[name] = index
    found.definitions.update(range(index, closing + 1))


def _rule_tokens(
    tokens: tuple[Token, ...],
    heads: dict[str, int],
) -> tuple[set[int], set[int], dict[int, str]]:
    """Return every definition's pattern, expansion and owned token indices."""
    patterns: set[int] = set()
    expansions: set[int] = set()
    owners: dict[int, str] = {}
    for name, index in heads.items():
        found = _definition_spans(tokens, index)
        if found is None:
            continue
        _, opening, closing = found
        body_patterns, body_expansions = _rule_spans(tokens, opening, closing)
        patterns.update(body_patterns)
        expansions.update(body_expansions)
        owners.update(dict.fromkeys(body_expansions, name))
    return patterns, expansions, owners


def _definition_spans(
    tokens: tuple[Token, ...],
    index: int,
) -> tuple[str, int, int] | None:
    """Return a ``macro_rules!`` definition's name and body span.

    ``index`` must point at the ``macro_rules`` token.  A head with no name or
    no body is not a definition this scan can read, so it contributes nothing.
    """
    if texts(tokens, index, len(_MACRO_HEAD)) != _MACRO_HEAD:
        return None
    name_index = index + len(_MACRO_HEAD)
    if name_index >= len(tokens) or tokens[name_index].kind is not Kind.IDENT:
        return None
    opening = name_index + 1
    closing = _group_end(tokens, opening, len(tokens))
    if closing is None:
        return None
    return tokens[name_index].text, opening, closing


def _macro_invocations(
    tokens: tuple[Token, ...],
    patterns: cabc.Container[int],
) -> dict[str, tuple[int, ...]]:
    """Return the token index of every invocation, grouped by macro name.

    An invocation is a name followed by ``!`` and a delimited body.  Matcher
    tokens are skipped, so a macro merely described there is not an invocation;
    an invocation written inside an expansion is kept, because a macro that
    calls another one still expands at the outer invocation's site.
    """
    invocations: dict[str, list[int]] = {}
    for index, token in enumerate(tokens[:-2]):
        if index in patterns or token.kind is not Kind.IDENT:
            continue
        if tokens[index + 1].text != "!":
            continue
        if tokens[index + 2].text not in _DELIMITER_PAIRS:
            continue
        invocations.setdefault(token.text, []).append(index)
    return {name: tuple(sites) for name, sites in invocations.items()}


def _rule_spans(
    tokens: tuple[Token, ...],
    opening: int,
    closing: int,
) -> tuple[set[int], set[int]]:
    """Return the pattern and expansion indices of each rule in a macro body.

    A group is a pattern when the token after it is ``=>``, and the group after
    the arrow is that rule's expansion. A rule whose arrow ends the body has no
    expansion to read.
    """
    patterns: set[int] = set()
    expansions: set[int] = set()
    index = opening + 1
    while index < closing:
        group = _group_end(tokens, index, closing)
        if group is None:
            index += 1
            continue
        if group + 1 < closing and tokens[group + 1].text == _RULE_ARROW:
            patterns.update(range(index, group + 1))
            expansion = _group_end(tokens, group + 2, closing)
            if expansion is not None:
                expansions.update(range(group + 2, expansion + 1))
        index = group + 1
    return patterns, expansions


def _group_end(tokens: tuple[Token, ...], index: int, limit: int) -> int | None:
    """Return the close index of the group opening at ``index``, if it opens one."""
    if index >= limit:
        return None
    closer = _DELIMITER_PAIRS.get(tokens[index].text)
    if closer is None:
        return None
    return matching(tokens, index, tokens[index].text, closer)
