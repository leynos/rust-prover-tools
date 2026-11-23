"""Resolve the local names a file binds to macros it defines.

A textual macro may be imported under another name, and an alias may itself be
renamed, so the binding is the least fixed point of the file's ``use``
statements.  Matcher text and builtin ``stringify!`` arguments import nothing;
the caller supplies those spans so the search skips them.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import begins_import, member_aliases

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("macro_aliases",)


def macro_aliases(
    tokens: tuple[Token, ...],
    heads: cabc.Container[str],
    *,
    patterns: cabc.Container[int],
    inert: cabc.Container[int],
) -> dict[str, str]:
    """Return every local name the file binds to a macro it defines.

    Rust expands ``use crate::make as build; build!();`` as the definition of
    ``make``, so the invocation site is the same site under another spelling.
    An alias can itself be renamed — ``use build as rebuild;`` — so discovery
    repeats until a pass adds no name, and each alias records the defined macro
    rather than the intermediate spelling.
    """
    renames = _all_macro_renames(tokens, patterns, inert)
    aliases: dict[str, str] = {}
    while True:
        fresh = _fresh_macro_aliases(renames, heads, aliases)
        for alias, target in fresh.items():
            if alias in heads and alias != target:
                msg = (
                    f"the local macro name {alias!r} is both a definition and "
                    f"an alias for {target!r}, so the audit cannot tell which "
                    "one a textual invocation expands"
                )
                raise TrustAuditError(msg)
        if not fresh:
            return aliases
        aliases.update(fresh)


def _fresh_macro_aliases(
    renames: cabc.Iterable[tuple[str, str]],
    heads: cabc.Container[str],
    aliases: cabc.Mapping[str, str],
) -> dict[str, str]:
    """Return this pass's newly resolved aliases, refusing a conflict.

    Two modules may bind one local name to different macros, and the per-file
    scan cannot tell which definition an invocation sees, so that ambiguity is
    refused rather than resolved arbitrarily.
    """
    fresh: dict[str, str] = {}
    for item, alias in sorted(renames):
        if item in heads:
            target = item
        elif item in aliases:
            target = aliases[item]
        else:
            continue
        known = aliases.get(alias, fresh.get(alias))
        if known is not None and known != target:
            msg = (
                f"the local macro name {alias!r} is bound to both {known!r} "
                f"and {target!r}, so the audit cannot tell which definition "
                "an invocation expands"
            )
            raise TrustAuditError(msg)
        if alias not in aliases:
            fresh[alias] = target
    return fresh


def _all_macro_renames(
    tokens: tuple[Token, ...],
    patterns: cabc.Container[int],
    inert: cabc.Container[int],
) -> frozenset[tuple[str, str]]:
    """Return every ``(item, alias)`` a ``use`` statement in the file renames.

    A macro is renamed either as a member (``use crate::make as build;``) or as
    the statement's root (``use build as rebuild;``), so both readings are
    collected.  Matcher text and stringified arguments bind nothing.
    """
    renames: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if index in patterns or index in inert:
            continue
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        renames.update(
            (root, alias)
            for root, alias in use_roots(tokens, index + 1, end)
            if alias is not None
        )
        renames.update(member_aliases(tokens, index + 1, end))
    return frozenset(renames)
