"""Refuse a module declaration a macro may place inside an inline module.

A macro that splices its arguments can wrap them in an inline module:
``wrap! { mod child; }`` expanding ``mod nested { $($t)* }`` compiles
``nested/child.rs``.  The per-file scan does not substitute arguments into the
expansion, so the declaration at the invocation site would be recorded under the
invocation's own chain and name a different file.  That placement is refused
rather than guessed.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.inline_modules import (
    DECLARATION_TOKENS,
    names_module,
)
from rust_prover_tools.trust_audit.structure import matching
from rust_prover_tools.trust_audit.syntax import Kind, Token

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.trust_audit.macro_rules import MacroRules

__all__ = ("refuse_forwarded_modules",)

# The delimiter pairs a macro invocation's body may open.
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}


def refuse_forwarded_modules(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    aliases: cabc.Mapping[str, str],
) -> None:
    """Refuse a module declaration passed into an argument-forwarding macro.

    Raises
    ------
    TrustAuditError
        If a module declaration lies in the arguments of a local macro whose
        expansion splices a metavariable.
    """
    for spelling, sites in rules.invocations.items():
        name = spelling if spelling in rules.heads else aliases.get(spelling)
        if name is None or not _forwards_arguments(tokens, rules, name):
            continue
        for site in sites:
            if site not in rules.patterns and _argument_declares_module(
                tokens,
                site,
            ):
                token = tokens[site]
                msg = (
                    f"the macro invocation at line {token.line}, column "
                    f"{token.column} passes a module declaration into {name!r}, "
                    "which may place it inside an inline module"
                )
                raise TrustAuditError(msg)


def _forwards_arguments(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    name: str,
) -> bool:
    """Report whether a macro's expansion splices a metavariable."""
    return any(
        index not in rules.patterns
        and index >= 1
        and tokens[index - 1].text == "$"
        and tokens[index].kind is Kind.IDENT
        for index in rules.expansions_by_name.get(name, ())
    )


def _argument_declares_module(tokens: tuple[Token, ...], site: int) -> bool:
    """Report whether an invocation's argument group holds a module declaration."""
    opening = site + 2
    if opening >= len(tokens) or tokens[opening].text not in _DELIMITER_PAIRS:
        return False
    closing = matching(
        tokens,
        opening,
        tokens[opening].text,
        _DELIMITER_PAIRS[tokens[opening].text],
    )
    return any(
        names_module(tokens[index], tokens[index + 1 : index + DECLARATION_TOKENS])
        for index in range(opening + 1, closing)
    )
