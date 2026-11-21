"""Derive the local names a file's imports bind to trusted items.

The matcher asks two questions of a file's imports: *which local names stand
for a trust root*, and *which stand for a member of one*. Both answers are the
least fixpoint of the import statements, because an alias may itself be renamed
before it is used, so they are computed here rather than alongside the
vocabulary that decides which roots and members matter.

`alias_bindings` reports crate aliases, which keep their qualified-path
meaning; `member_bindings` reports member aliases, which are trust only when
used bare. Reading the statements once would miss a second rename and report
the file as trusting nothing at that use.
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

__all__ = ("alias_bindings", "member_bindings")

# ``extern``, ``crate``, the crate's name, ``as``, and the local name.
_EXTERN_CRATE_TOKENS = 5


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


def member_bindings(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
) -> frozenset[tuple[str, str]]:
    """Return ``(item, local name)`` for each renamed import from ``roots``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    roots : Container[str]
        The crate names whose members are followed, including any local name
        the file binds one of them to.

    Returns
    -------
    frozenset[tuple[str, str]]
        One ``(item, local name)`` pair per binding, where ``item`` is the
        member's own name and ``local name`` is the spelling the file uses for
        it.  Every pair a rename can reach is included, so one local name may
        appear in several pairs.

    Notes
    -----
    ``use kani::{assume as a};`` binds ``a`` to Kani's ``assume``, so a later
    ``a(x)`` is the same request for trust under another spelling. Unlike a
    crate alias, the local name stands for one item: only a bare use of it is
    trust, and a path through it is not, because a member is not a crate.

    A rename of the crate itself — ``use kani as k;`` or
    ``use kani::{self as k};`` — is not a member binding, and ``self`` is not an
    item the vocabulary names. `alias_bindings` reports those instead.

    A member can be renamed again: after ``use kani::{assume as a};``, the
    statement ``use a as b;`` binds ``b`` to the same item. The search
    therefore repeats until a pass adds no pair, just as `alias_bindings` does
    for crates; reading the statements once would miss ``b(x)`` and report the
    file as trusting nothing at that use.

    One local name may stand for several items. Rust scopes allow ``use
    kani::assume as m;`` in one module and ``use kani::any_where as m;`` in
    another, and the scan is file-scoped, so it reports every item ``m`` may
    denote rather than keeping whichever scope happened to be read last.

    Examples
    --------
    ``use kani::{assume as a};`` reports ``(('assume', 'a'),)``.
    """
    bindings: dict[str, set[str]] = {}
    while True:
        # The pairs already explained, so a round that only re-reports them
        # ends the search.  Extending with the rest would leave the loop with
        # work to do forever.
        known = {(alias, item) for alias, items in bindings.items() for item in items}
        discovered = _binding_round(tokens, roots, bindings)
        fresh = {
            (alias, item)
            for alias, items in discovered.items()
            for item in items
            if (alias, item) not in known
        }
        if not fresh:
            return frozenset((item, alias) for alias, item in known)
        _merge(bindings, discovered)


def _binding_round(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
    bindings: cabc.Mapping[str, cabc.Iterable[str]],
) -> dict[str, set[str]]:
    """Return the names one pass binds to items, keyed by the local name.

    A path rooted at one of ``roots`` names a member of that crate. A path
    rooted at a name ``bindings`` already maps to items is a further rename of
    those same items, which Rust permits as ``use alias as another;``.
    """
    discovered: dict[str, set[str]] = {}
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or token.text != "use":
            continue
        end = statement_end(tokens, index + 1)
        for span in rooted_spans(tokens, index + 1, end):
            _merge(discovered, _span_bindings(tokens, span, roots, bindings))
    return discovered


def _span_bindings(
    tokens: tuple[Token, ...],
    span: tuple[str, int, int],
    roots: cabc.Container[str],
    bindings: cabc.Mapping[str, cabc.Iterable[str]],
) -> dict[str, set[str]]:
    """Return the names one path of a ``use`` statement binds to which items.

    Three shapes name items.  A path rooted at a trust root names that crate's
    members.  A path rooted at a local name already bound to items is a further
    rename of those items.  A path rooted at ``self``, ``crate`` or ``super``
    reaches the binding through the module it lives in: ``use self::a as b;``
    after ``use kani::assume as a;`` binds ``b`` to Kani's ``assume`` just as
    ``use a as b;`` does, and the same holds through ``crate::`` and
    ``super::`` from a nested module.
    """
    root, start, stop = span
    if root in roots:
        return _member_bindings(tokens, start, stop)
    if root in bindings:
        return _renamed_bindings(tokens, start, stop, bindings[root])
    if root in LOCAL_QUALIFIERS:
        return _requalified_bindings(tokens, start, stop, bindings)
    return {}


def _member_bindings(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
) -> dict[str, set[str]]:
    """Return the trust-root members one path binds, keyed by local name.

    A plain import binds the member under its own name, so it seeds the map.
    Without that seed, ``use kani::assume; use self::assume as a;`` has no
    entry for ``assume`` and the second binding is lost, leaving a later
    ``a(..)`` unreported.
    """
    discovered: dict[str, set[str]] = {}
    for member, alias in member_aliases(tokens, start, stop):
        discovered.setdefault(alias, set()).add(member)
    plain = _plain_member(tokens, start, stop)
    if plain is not None:
        discovered.setdefault(plain, set()).add(plain)
    return discovered


def _plain_member(tokens: tuple[Token, ...], start: int, stop: int) -> str | None:
    """Return the member a path imports without a rename, if it names one.

    The last identifier before any ``as`` is the member; a path ending in a
    glob names none.  A path that names only the crate roots itself, which the
    vocabulary does not list, so seeding it is harmless.
    """
    end = stop
    for index in range(start, stop):
        if tokens[index].text == "as":
            end = index
            break
    for index in range(end - 1, start - 1, -1):
        if tokens[index].kind is Kind.IDENT:
            return tokens[index].text
    return None


def _renamed_bindings(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
    items: cabc.Iterable[str],
) -> dict[str, set[str]]:
    """Return the names a further rename of ``items`` binds to those items."""
    discovered: dict[str, set[str]] = {}
    for _root, alias in use_roots(tokens, start, stop):
        if alias is not None:
            discovered.setdefault(alias, set()).update(items)
    return discovered


def _requalified_bindings(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
    bindings: cabc.Mapping[str, cabc.Iterable[str]],
) -> dict[str, set[str]]:
    """Return the names a ``self::``, ``crate::`` or ``super::`` rename binds.

    The member segment names a local binding rather than a module, so the
    rename reaches whatever items that binding already denotes.  A member with
    no local binding names something this scan never saw, and is left alone.
    """
    discovered: dict[str, set[str]] = {}
    for member, alias in member_aliases(tokens, start, stop):
        if member in bindings:
            discovered.setdefault(alias, set()).update(bindings[member])
    return discovered


def _merge(target: dict[str, set[str]], extra: dict[str, set[str]]) -> None:
    """Add every ``(name, item)`` pair in ``extra`` to ``target``."""
    for alias, items in extra.items():
        target.setdefault(alias, set()).update(items)


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
