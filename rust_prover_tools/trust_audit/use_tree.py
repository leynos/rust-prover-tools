"""Read the ``use`` statements that decide a trust root's local names.

The audit answers one question: *which names in this file reach a verifier's
trust surface?* The roots that answer it are crates — ``kani``, ``vstd``,
``verus_builtin`` — but a Rust file need not spell the crate at the use site.
Three forms change what a name refers to:

- ``use kani::assume;`` binds a *name*, so a bare ``assume(..)`` later in the
  file is the Kani call even without the ``kani::`` prefix.
- ``use kani as k;`` binds the *crate* under another name, so ``k::assume`` is
  the same qualified path as ``kani::assume``.
- ``use kani::*;`` binds whatever the crate exports, and is treated the same
  way as the named form for the fail-closed reason the matcher states.

The helpers here are deliberately narrow: they locate the statement, its root,
and an optional alias. Deciding what a binding *means* belongs to
`rust_prover_tools.trust_audit.matcher`, which owns the vocabulary; this module
only reports what the source says.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_alias import crate_alias_indices

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "member_bindings", "statement_end", "use_roots")

# ``extern``, ``crate``, the crate's name, ``as``, and the local name.
_EXTERN_CRATE_TOKENS = 5


def alias_bindings(tokens: tuple[Token, ...], root: str) -> frozenset[str]:
    """Return every local name a file binds ``root`` to, plus ``root`` itself.

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
    return discovered


def member_bindings(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
) -> frozenset[tuple[str, str]]:
    """Return ``(item, local name)`` for each renamed import from ``roots``.

    ``use kani::{assume as a};`` binds ``a`` to Kani's ``assume``, so a later
    ``a(x)`` is the same request for trust under another spelling. Unlike a
    crate alias, the local name stands for one item: only a bare use of it is
    trust, and a path through it is not, because a member is not a crate.

    A rename of the crate itself — ``use kani as k;`` or
    ``use kani::{self as k};`` — is not a member binding, and ``self`` is not an
    item the vocabulary names. `alias_bindings` reports those instead.

    A member can be renamed again: after ``use kani::{assume as a};``, the
    statement ``use a as b;`` binds ``b`` to the same item. The search
    therefore repeats until a pass adds no name, just as `alias_bindings` does
    for crates; reading the statements once would miss ``b(x)`` and report the
    file as trusting nothing at that use.

    Examples
    --------
    ``use kani::{assume as a};`` reports ``(('assume', 'a'),)``.
    """
    bindings: dict[str, str] = {}
    while True:
        # A round re-reports every binding it can see, so only the names it
        # newly explains may extend the map.  Extending with the rest would
        # leave the loop with work to do forever.
        discovered = {
            alias: item
            for alias, item in _binding_round(tokens, roots, bindings).items()
            if bindings.get(alias) != item
        }
        if not discovered:
            return frozenset((item, alias) for alias, item in bindings.items())
        bindings.update(discovered)


def _binding_round(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
    bindings: cabc.Mapping[str, str],
) -> dict[str, str]:
    """Return the names one pass binds to an item, keyed by the local name.

    A path rooted at one of ``roots`` names a member of that crate. A path
    rooted at a name ``bindings`` already maps to an item is a further rename
    of that same item, which Rust permits as ``use alias as another;``.
    """
    discovered: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or token.text != "use":
            continue
        end = statement_end(tokens, index + 1)
        for root, start, stop in _rooted_spans(tokens, index + 1, end):
            if root in roots:
                discovered.update(
                    (alias, member)
                    for member, alias in _member_aliases(tokens, start, stop)
                )
            elif root in bindings:
                discovered.update(
                    (alias, bindings[root])
                    for _root, alias in use_roots(tokens, start, stop)
                    if alias is not None
                )
    return discovered


def use_roots(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> tuple[tuple[str, str | None], ...]:
    """Return every crate a ``use`` statement takes paths from, and its alias.

    One statement can name several crates. ``use {std::mem, kani as k};`` is the
    same pair of imports as two statements, and each half carries its own alias,
    so a reader that reported a single root lost ``k`` entirely: with ``std``
    first, ``k::assume`` was never matched and the audit described a file as
    trust-free while it assumed exactly what the vocabulary names.

    A statement that *starts* with a brace names several roots; one that starts
    with an identifier names one root, and any later brace lists that root's
    members. Groups nest, so a member may itself be a brace list, and the answer
    is the same set of roots however deeply the braces are nested.

    Examples
    --------
    ``use kani as k;`` reports ``(('kani', 'k'),)``, and
    ``use {std::mem, kani as k};`` reports ``(('std', None), ('kani', 'k'))``.
    """
    if _is_grouped(tokens, start, end):
        return _grouped_roots(tokens, start, end)
    root = use_root(tokens, start)
    if root is None:
        return ()
    return tuple((root, alias) for alias in use_aliases(tokens, start, end)) or (
        (root, None),
    )


def use_root(tokens: tuple[Token, ...], start: int) -> str | None:
    """Return the crate a ``use`` statement takes its paths from.

    A leading ``::`` means an absolute path, so the root is the first identifier
    after it. Anything else is the first identifier in the statement. A leading
    ``{`` is a group rather than a root, so it is not read as one: ``use
    {std::mem, kani};`` is two paths, and `use_roots` splits it before this
    function is asked about a member.
    """
    for index in range(start, statement_end(tokens, start)):
        token = tokens[index]
        if token.text == "{":
            continue
        if token.kind is Kind.IDENT:
            return token.text
    return None


def use_aliases(tokens: tuple[Token, ...], start: int, end: int) -> frozenset[str]:
    """Return every name that renames the crate itself in one ``use`` statement.

    Two forms rename the crate. The plain ``use root as name;`` does it
    directly, and the grouped ``use root::{self as name};`` does it through the
    ``self`` member, which stands for the crate itself rather than for one of
    its items.

    A rename of any other member — ``use root::{assume as a};`` — is not this
    statement's own alias. Treating it as one would match every ``a::`` path in
    the file against the trust root, which is a claim the source does not make.

    Examples
    --------
    ``use kani as k;`` reports ``{'k'}``, while ``use kani::{assume as a};``
    reports the empty set because ``a`` is a member's name rather than the
    crate's.
    """
    return frozenset(
        tokens[index + 1].text for index in crate_alias_indices(tokens, start, end)
    )


def statement_end(tokens: tuple[Token, ...], start: int) -> int:
    """Return the index of the ``;`` closing the statement opened at ``start``."""
    for index in range(start, len(tokens)):
        if tokens[index].text == ";":
            return index
    return len(tokens)


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


def _rooted_spans(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[str, int, int]]:
    """Yield ``(root, start, end)`` for each leaf path of one ``use`` statement.

    A brace holds one path per member, each of which may name its own crate, so
    each member is reported with the root governing *it* rather than with the
    first root in the statement: ``use {std::x as X, kani::assume as a};``
    renames ``a`` out of Kani and ``X`` out of the standard library.
    """
    if _is_grouped(tokens, start, end):
        for member_start, member_end in _top_level_members(tokens, start, end):
            yield from _rooted_spans(tokens, member_start, member_end)
        return
    root = use_root(tokens, start)
    if root is not None:
        yield root, start, end


def _member_aliases(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[str, str]]:
    """Yield ``(item, local name)`` for each member rename in one rooted path.

    The crate's own rename is left out. A member rename is any other ``as``
    with an identifier on each side, so ``kani::foo::assume as a`` and
    ``kani::{assume as a, assert as b}`` are read alike.
    """
    crate_aliases = set(crate_alias_indices(tokens, start, end))
    for index in range(start + 1, end - 1):
        if index in crate_aliases or tokens[index].text != "as":
            continue
        member, alias = tokens[index - 1], tokens[index + 1]
        if member.kind is Kind.IDENT and alias.kind is Kind.IDENT:
            yield member.text, alias.text


def _is_grouped(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether the statement opens with a brace of roots.

    A leading ``::`` is path syntax rather than a root, so it is stepped over
    before asking what the statement starts with.
    """
    index = start
    while index < end and tokens[index].text == "::":
        index += 1
    return index < end and tokens[index].text == "{"


def _grouped_roots(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> tuple[tuple[str, str | None], ...]:
    """Read each top-level member of ``use {..}`` as its own rooted path.

    A member can itself be a group. The reference permits nested use trees, so
    ``use {{std::mem, kani as k}};`` is the same two imports one brace deeper
    than a flat list; recursing keeps every crate and its alias at any depth.

    A crate may also be renamed more than once, as in
    ``use kani::{self as first, self as second};``, which binds both names. Each
    alias is reported as its own pair, so a reader that kept only the first
    would drop ``second`` and miss ``second::assume``.
    """
    roots = []
    for member_start, member_end in _top_level_members(tokens, start, end):
        if _is_grouped(tokens, member_start, member_end):
            roots.extend(_grouped_roots(tokens, member_start, member_end))
            continue
        root = use_root(tokens, member_start)
        if root is None:
            continue
        aliases = use_aliases(tokens, member_start, member_end)
        roots.extend((root, alias) for alias in aliases or (None,))
    return tuple(roots)


def _top_level_members(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[int, int]]:
    """Yield the token span of each comma-separated member inside the brace.

    Only depth 1 splits: a nested ``use {kani::{assume, self as k}, std};`` must
    keep ``kani::`` together, and splitting on every comma would read ``self``
    as a crate named ``self``.
    """
    depth = 0
    member_start = None
    for index in range(start, end):
        text = tokens[index].text
        if text == "{":
            depth += 1
            member_start = index + 1 if depth == 1 else member_start
        elif text == "}":
            if depth == 1 and member_start is not None:
                yield member_start, index
            depth -= 1
        elif text == "," and depth == 1:
            if member_start is not None:
                yield member_start, index
            member_start = index + 1
