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

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.crate_bindings import (
    alias_bindings,
)
from rust_prover_tools.trust_audit.crate_bindings import (
    merge as _merge,
)
from rust_prover_tools.trust_audit.structure import matching
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import (
    is_grouped,
    statement_end,
    top_level_members,
    use_root,
    use_roots,
)
from rust_prover_tools.trust_audit.use_tree import (
    LOCAL_QUALIFIERS,
    begins_import,
    member_aliases,
    rooted_spans,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "member_bindings")


@dc.dataclass(frozen=True, slots=True)
class _Scope:
    """The crate roots and member bindings one pass reads a statement against."""

    roots: cabc.Container[str]
    bindings: cabc.Mapping[str, cabc.Iterable[str]]


# ``extern``, ``crate``, the crate's name, ``as``, and the local name.
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
    scope = _Scope(roots, bindings)
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        for span in rooted_spans(tokens, index + 1, end):
            _merge(discovered, _span_bindings(tokens, span, scope))
    return discovered


def _span_bindings(
    tokens: tuple[Token, ...],
    span: tuple[str, int, int],
    scope: _Scope,
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
    discovered: dict[str, set[str]] = {}
    if root in scope.roots:
        _merge(discovered, _member_bindings(tokens, start, stop))
    if root in scope.bindings:
        _merge(
            discovered,
            _renamed_bindings(tokens, start, stop, scope.bindings[root]),
        )
    if root in LOCAL_QUALIFIERS:
        _merge(discovered, _requalified_bindings(tokens, start, stop, scope))
    return discovered


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
    for plain in _plain_members(tokens, start, stop):
        discovered.setdefault(plain, set()).add(plain)
    return discovered


def _plain_members(tokens: tuple[Token, ...], start: int, stop: int) -> frozenset[str]:
    """Return every member a path imports without a rename.

    A grouped path binds one member per leaf, so ``use kani::{assume, any};``
    binds both.  Reading only the last identifier in the span would seed
    ``any`` and lose ``assume``, and a later ``use self::assume as a;`` would
    then never reach the item it renames.
    """
    opener = _group_opener(tokens, start, stop)
    if opener is None:
        member = _plain_member(tokens, start, stop)
        return frozenset() if member is None else frozenset({member})
    closer = matching(tokens, opener, "{", "}")
    members: set[str] = set()
    for member_start, member_end in top_level_members(tokens, opener, closer + 1):
        members.update(_plain_members(tokens, member_start, member_end))
    return frozenset(members)


def _group_opener(tokens: tuple[Token, ...], start: int, stop: int) -> int | None:
    """Return the index of the brace a use path opens, if it opens one."""
    for index in range(start, stop):
        if tokens[index].text == "{":
            return index
    return None


def _plain_member(tokens: tuple[Token, ...], start: int, stop: int) -> str | None:
    """Return the member a path imports without a rename, if it names one.

    A renamed import binds only the new name: ``use kani::assume as a;`` binds
    ``a``, not ``assume``, and seeding ``assume`` would let an unrelated
    ``use self::assume as b;`` be read as Kani's item.  A path ending in a glob
    names no member either.  A path that names only the crate roots itself,
    which the vocabulary does not list, so seeding it is harmless.
    """
    if any(tokens[index].text == "as" for index in range(start, stop)):
        return None
    for index in range(stop - 1, start - 1, -1):
        if tokens[index].kind is Kind.IDENT:
            member = tokens[index].text
            # ``self`` in ``use kani::{self, assume};`` stands for the crate,
            # not for a member.  Seeding it would shadow the local-qualifier
            # dispatch and lose the member a later ``use self::…`` renames.
            return None if member in LOCAL_QUALIFIERS else member
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
    scope: _Scope,
) -> dict[str, set[str]]:
    """Return the names a ``self::``, ``crate::`` or ``super::`` rename binds.

    Two shapes reach a binding.  ``use self::a as b;`` names a local member
    binding, so it reaches whatever items ``a`` already denotes.
    ``use self::k::assume as a;`` names a crate alias first: once ``k`` is a
    known alias for a trust root, the rest of the path is an ordinary member
    path, and the import binds ``assume`` just as ``use k::assume as a;`` does.
    A member with no local binding names something this scan never saw, and is
    left alone.
    """
    discovered: dict[str, set[str]] = {}
    qualified_start = _after_qualifiers(tokens, start, stop)
    if qualified_start < stop and tokens[qualified_start].text == "{":
        return _grouped_requalified_bindings(tokens, qualified_start, scope)
    qualified = _qualified_root_member(tokens, start, stop)
    if qualified is not None:
        member_index, member = qualified
        if member in scope.roots:
            _merge(discovered, _member_bindings(tokens, member_index, stop))
    for member, alias in member_aliases(tokens, start, stop):
        if member in scope.bindings:
            discovered.setdefault(alias, set()).update(scope.bindings[member])
    return discovered


def _qualified_root_member(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
) -> tuple[int, str] | None:
    """Return the first member a local qualifier names, and its index.

    ``self::k::assume`` yields ``k`` at its own index: the segment a crate
    alias would occupy.  ``super`` may repeat — ``use super::super::k::assume``
    is valid Rust — so every leading qualifier is skipped.  A path that stops
    at a qualifier names no member.
    """
    index = _after_qualifiers(tokens, start, stop)
    if index >= stop or tokens[index].kind is not Kind.IDENT:
        return None
    return index, tokens[index].text


def _after_qualifiers(tokens: tuple[Token, ...], start: int, stop: int) -> int:
    """Return the index after every leading local qualifier."""
    index = start
    while index < stop and tokens[index].text == "::":
        index += 1
    while (
        index + 1 < stop
        and tokens[index].text in LOCAL_QUALIFIERS
        and tokens[index + 1].text == "::"
    ):
        index += 2
    return index


def _grouped_requalified_bindings(
    tokens: tuple[Token, ...],
    opener: int,
    scope: _Scope,
) -> dict[str, set[str]]:
    """Bind each member of ``self::{…}`` as if the qualifiers prefixed it.

    A member may itself be a group — ``use self::{{unrelated, a as b}};`` is
    the same import one brace deeper — so the walk recurses rather than reading
    the inner brace as the first segment of a path.
    """
    closing = matching(tokens, opener, "{", "}")
    discovered: dict[str, set[str]] = {}
    for member_start, member_end in top_level_members(tokens, opener, closing + 1):
        if is_grouped(tokens, member_start, member_end):
            _merge(
                discovered,
                _grouped_requalified_bindings(tokens, member_start, scope),
            )
            continue
        root = use_root(tokens, member_start, member_end)
        if root is None:
            continue
        if root in scope.roots:
            _merge(discovered, _member_bindings(tokens, member_start, member_end))
        elif root in scope.bindings:
            _merge(
                discovered,
                _renamed_bindings(
                    tokens,
                    member_start,
                    member_end,
                    scope.bindings[root],
                ),
            )
    return discovered
