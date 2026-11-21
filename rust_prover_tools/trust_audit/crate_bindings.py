"""Follow the names a ``use`` statement binds a crate to.

A crate may be renamed on import (``use kani as k;``), renamed again through a
local qualifier (``use self::k as kk;``), or renamed by ``extern crate``.  The
matcher treats every resulting name exactly like the literal crate root, so the
search here repeats until a pass adds no name.  Member bindings live in
`rust_prover_tools.trust_audit.use_bindings`.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import (
    LOCAL_QUALIFIERS,
    member_aliases,
    rooted_spans,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "merge")

# ``extern``, ``crate``, the crate's name, ``as``, and the local name.
_EXTERN_CRATE_TOKENS = 5


def merge(target: dict[str, set[str]], extra: dict[str, set[str]]) -> None:
    """Add every ``(name, item)`` pair in ``extra`` to ``target``."""
    for alias, items in extra.items():
        target.setdefault(alias, set()).update(items)


def alias_bindings(tokens: tuple[Token, ...], root: str) -> frozenset[str]:
    """Return every local name a file binds ``root`` to, plus ``root`` itself.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    root : str
        The crate name to follow, such as ``kani``.

    Returns
    -------
    frozenset[str]
        ``root`` and every name the file binds it to, including a name bound
        only through a rename of a rename.

    Notes
    -----
    Rust lets a crate be renamed on import — ``use kani as k;`` — after which
    ``k::assume`` is the same call as ``kani::assume``. The audit's location
    claim is about what the code does, not what the crate is called at the use
    site, so an alias is matched exactly like the literal root.

    A grouped import can rename the crate as well, through the ``self`` member:
    ``use kani::{self as k};`` binds ``k`` as another name for the crate. An
    import that merely renames a *member* — ``use kani::{assume as a};`` — still
    binds no crate alias: the ``as`` there belongs to ``assume``, and reading it
    as one would make every ``a::`` path trust.

    A glob import (``use kani::*;``) binds items rather than the crate, so the
    qualified-path rule does not apply to it.

    ``extern crate`` renames the crate the same way and without a ``use``:
    ``extern crate kani as k;`` makes ``k::assume`` the Kani call. Missing it
    would describe a file as trust-free while it assumed exactly what the
    vocabulary names, which is the fail-open direction this module exists to
    prevent.

    An alias can itself be renamed — ``use kani as k; use k as kk;`` — so the
    search repeats until a pass adds no name. Reading the statements once
    would miss ``kk::assume`` and report the file as trusting nothing.

    Examples
    --------
    A file that renames the crate reports both names: ``use kani as k;`` binds
    ``k`` and ``kani``. An ``extern crate kani as k;`` reports the same pair.
    """
    names = {root}
    while True:
        discovered = _alias_round(tokens, names)
        if discovered <= names:
            return frozenset(names)
        names |= discovered


def _alias_round(
    tokens: tuple[Token, ...],
    names: cabc.Container[str],
) -> set[str]:
    """Return every name one pass binds to a crate already in ``names``."""
    discovered: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT:
            continue
        declared = _extern_crate_alias(tokens, index)
        if declared is not None:
            crate, alias = declared
            if crate in names:
                discovered.add(alias)
            continue
        if token.text != "use":
            continue
        end = statement_end(tokens, index + 1)
        discovered.update(
            alias
            for crate, alias in use_roots(tokens, index + 1, end)
            if crate in names and alias is not None
        )
        discovered.update(_requalified_aliases(tokens, index + 1, end, names))
    return discovered


def _requalified_aliases(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
    names: cabc.Container[str],
) -> set[str]:
    """Return crate aliases bound through ``self::``, ``crate::`` or ``super::``.

    ``use kani as k; use self::k as kk;`` binds ``kk`` to the same crate, so a
    later ``kk::assume`` is Kani's.  The qualifier names the module holding the
    binding, not a crate, and the member names the alias already in ``names``.
    The fixpoint in `alias_bindings` makes the order of the two statements
    irrelevant.
    """
    discovered: set[str] = set()
    for root, span_start, span_stop in rooted_spans(tokens, start, end):
        if root not in LOCAL_QUALIFIERS:
            continue
        discovered.update(
            alias
            for member, alias in member_aliases(tokens, span_start, span_stop)
            if member in names
        )
    return discovered


def _extern_crate_alias(
    tokens: tuple[Token, ...],
    start: int,
) -> tuple[str, str] | None:
    """Return the crate and name ``extern crate <crate> as <name>;`` binds.

    ``None`` covers every other shape, including an ``extern crate`` without a
    rename: that spelling binds the crate under its own name, which is already
    in the result set.
    """
    head = tokens[start : start + _EXTERN_CRATE_TOKENS]
    if len(head) < _EXTERN_CRATE_TOKENS:
        return None
    if tuple(token.text for token in head[:2]) != ("extern", "crate"):
        return None
    if head[2].kind is not Kind.IDENT:
        return None
    if head[3].text != "as" or head[4].kind is not Kind.IDENT:
        return None
    return head[2].text, head[4].text
