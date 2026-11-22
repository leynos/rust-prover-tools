"""Resolve the member paths one ``use`` statement binds.

`rust_prover_tools.trust_audit.use_bindings` repeats a pass over the file until
the bindings stop growing; each pass hands a rooted path to `span_bindings`
here.  A path may name a trust crate's member, a further rename of a local
name, or a member reached through ``self::``, ``crate::`` or ``super::``.  The
same helpers also cover a member a declared module re-exports and a member a
glob supplies, because both reach the file's local names without naming the
crate at the use site.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.crate_bindings import merge as _merge
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

__all__ = ("Scope", "span_bindings")


@dc.dataclass(frozen=True, slots=True)
class Scope:
    """The crate roots and member bindings one pass reads a statement against."""

    roots: cabc.Container[str]
    bindings: cabc.Mapping[str, cabc.Iterable[str]]


def span_bindings(
    tokens: tuple[Token, ...],
    span: tuple[str, int, int],
    scope: Scope,
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
    # A module this file declares may re-export a member binding, and the
    # group preserves the path prefix: ``use m::a as b;`` reaches the leaf
    # ``a`` the file already bound even though ``m`` is not a trust root.  The
    # same reading applies to every rooted path, so the leaf is checked here.
    for member, alias in member_aliases(tokens, start, stop):
        if member in scope.bindings:
            discovered.setdefault(alias, set()).update(scope.bindings[member])
    # A trust crate can also appear after a module segment the scan cannot
    # resolve: ``use bridge::k::assume as a;`` names the member through the
    # alias ``k``.  The leaf is bound whenever a segment before it is a trust
    # root, not only when the path begins with one.
    for index in range(start + 1, stop - 1):
        if _names_root(tokens, index, scope.roots):
            _merge(discovered, _member_bindings(tokens, index, stop))
            break
    return discovered


def _names_root(
    tokens: tuple[Token, ...],
    index: int,
    roots: cabc.Container[str],
) -> bool:
    """Report whether the segment at ``index`` names a trust root and continues."""
    token = tokens[index]
    if token.kind is not Kind.IDENT or token.text not in roots:
        return False
    return tokens[index + 1].text == "::"


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
    scope: Scope,
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
    glob_root = _has_root_glob(tokens, scope.roots)
    for member, alias in member_aliases(tokens, start, stop):
        if member in scope.bindings:
            discovered.setdefault(alias, set()).update(scope.bindings[member])
        elif glob_root:
            # A trust-root glob may bind the member under its own name, so a
            # local qualifier that renames it names the same item.
            discovered.setdefault(alias, set()).add(member)
    return discovered


def _has_root_glob(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
) -> bool:
    """Report whether the file glob-imports from one of the trust roots."""
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        for root, start, stop in rooted_spans(tokens, index + 1, end):
            if root in roots and any(
                tokens[pos].text == "*" for pos in range(start, stop)
            ):
                return True
    return False


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
    scope: Scope,
) -> dict[str, set[str]]:
    """Bind each member of ``self::{…}`` as if the qualifiers prefixed it.

    A member may itself be a group — ``use self::{{unrelated, a as b}};`` is
    the same import one brace deeper — so the walk recurses rather than reading
    the inner brace as the first segment of a path.
    """
    closing = matching(tokens, opener, "{", "}")
    discovered: dict[str, set[str]] = {}
    glob_root = _has_root_glob(tokens, scope.roots)
    for member_start, member_end in top_level_members(tokens, opener, closing + 1):
        _merge(
            discovered,
            _grouped_member_bindings(
                tokens,
                (member_start, member_end),
                scope,
                glob_root=glob_root,
            ),
        )
    return discovered


def _grouped_member_bindings(
    tokens: tuple[Token, ...],
    span: tuple[int, int],
    scope: Scope,
    *,
    glob_root: bool,
) -> dict[str, set[str]]:
    """Bind one member of ``self::{…}`` as if the qualifiers prefixed it."""
    start, stop = span
    if is_grouped(tokens, start, stop):
        return _grouped_requalified_bindings(tokens, start, scope)
    discovered: dict[str, set[str]] = {}
    root = use_root(tokens, start, stop)
    if root is None:
        return discovered
    if root in scope.roots:
        _merge(discovered, _member_bindings(tokens, start, stop))
    elif root in scope.bindings:
        _merge(
            discovered,
            _renamed_bindings(tokens, start, stop, scope.bindings[root]),
        )
    # A group preserves the path prefix, so ``inner::a as b`` reaches the leaf
    # ``a`` the file already bound even when ``inner`` is a module this scan
    # cannot resolve.  Reading only the first segment would drop the binding
    # the group exists to make.
    for member, alias in member_aliases(tokens, start, stop):
        if member in scope.bindings:
            discovered.setdefault(alias, set()).update(scope.bindings[member])
        elif glob_root:
            discovered.setdefault(alias, set()).add(member)
    return discovered
