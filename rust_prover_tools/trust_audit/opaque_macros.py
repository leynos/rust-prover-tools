"""Track the opaque macro names, including the names they are re-exported as.

``include!``, ``paste!`` and ``concat_idents!`` generate code the lexer never
sees.  A ``use`` rename is the same macro under another spelling, and a local
module can re-export the name before a second rename, so the search follows
every rename in the file to a fixed point.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import begins_import, member_aliases

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("opaque_names",)


def opaque_names(
    tokens: tuple[Token, ...],
    base: frozenset[str],
    patterns: cabc.Container[int],
    *,
    inert: cabc.Container[int] = frozenset(),
) -> frozenset[str]:
    """Return the opaque macro names, plus every local name bound to one.

    A re-export can pass a name through more than one module —
    ``mod m { pub use std::include as load; } use m::load as hidden;`` — so the
    search reads every ``as`` rename in the file and repeats until a pass adds
    nothing.  Following only the roots of the statements would record
    ``include → load`` and ``load → hidden`` but never ``include → hidden``,
    and ``hidden!`` would be read as an ordinary macro.  A ``use`` written in a
    matcher is input, not an import, so those tokens are skipped, and so is one
    written inside the builtin ``stringify!``, whose argument becomes text.
    """
    names = set(base)
    renames = _all_renames(tokens, patterns, inert)
    while True:
        discovered = {alias for item, alias in renames if item in names}
        if discovered <= names:
            return frozenset(names)
        names |= discovered


def _all_renames(
    tokens: tuple[Token, ...],
    patterns: cabc.Container[int],
    inert: cabc.Container[int],
) -> frozenset[tuple[str, str]]:
    """Return every ``(item, alias)`` a ``use`` statement in the file renames.

    A rename of a macro's item (``use std::include as load;``) and a rename of
    the name it bound (``use load as hidden;``) are both followed: the second
    spelling is the same opaque macro, and stopping at the first would let
    ``hidden!`` expand code the scan never reads.  Tokens inside a
    ``macro_rules!`` matcher, or inside a builtin ``stringify!`` argument,
    import nothing, so the aliases they spell bind no local name.
    """
    renames: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if index in patterns or index in inert:
            continue
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        renames.update(member_aliases(tokens, index + 1, end))
        renames.update(
            (root, alias)
            for root, alias in use_roots(tokens, index + 1, end)
            if alias is not None
        )
    return frozenset(renames)
