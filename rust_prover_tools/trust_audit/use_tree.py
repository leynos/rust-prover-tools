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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "statement_end", "use_roots")

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

    Examples
    --------
    A file that renames the crate reports both names: ``use kani as k;`` binds
    ``k`` and ``kani``. An ``extern crate kani as k;`` reports the same pair.
    """
    names = {root}
    for index, token in enumerate(tokens):
        renamed = _extern_crate_alias(tokens, index, root)
        if renamed is not None:
            names.add(renamed)
            continue
        if token.kind is not Kind.IDENT or token.text != "use":
            continue
        end = statement_end(tokens, index + 1)
        aliases = [
            alias for named, alias in use_roots(tokens, index + 1, end) if named == root
        ]
        names.update(alias for alias in aliases if alias is not None)
    return frozenset(names)


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
        tokens[index + 1].text for index in _crate_alias_indices(tokens, start, end)
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
    root: str,
) -> str | None:
    """Return the name ``extern crate <root> as <name>;`` binds at ``start``.

    ``None`` covers every other shape, including an ``extern crate`` without a
    rename: that spelling binds the crate under its own name, which is already
    in the result set.
    """
    head = tokens[start : start + _EXTERN_CRATE_TOKENS]
    if len(head) < _EXTERN_CRATE_TOKENS:
        return None
    if tuple(token.text for token in head[:3]) != ("extern", "crate", root):
        return None
    if head[3].text != "as" or head[4].kind is not Kind.IDENT:
        return None
    return head[4].text


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


def _crate_alias_indices(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[int]:
    """Yield the index of each ``as`` that renames the crate in one statement.

    At most one such ``as`` can appear in a statement Rust accepts, but the
    reader reports every one it finds rather than assuming: a statement it
    misreads is better reported twice than silently narrowed.
    """
    for index in range(start, end - 1):
        if tokens[index].text != "as":
            continue
        if tokens[index + 1].kind is not Kind.IDENT:
            continue
        if _renames_crate(tokens, start, index):
            yield index


def _renames_crate(tokens: tuple[Token, ...], start: int, as_index: int) -> bool:
    """Report whether the ``as`` at ``as_index`` renames the root, not a member.

    Two positions do so: directly after the root (``root as name``) and directly
    after a ``self`` that is itself the first thing inside a brace
    (``root::{self as name}``). Every other ``as`` in the statement belongs to a
    member path.

    A bare ``self`` is the root itself, so ``self as name`` renames the crate
    wherever it appears. Any other identifier is the root only when it is the
    first one in the statement: ``root::name as alias`` puts a second identifier
    in front of the ``as``, and that is a member rename.
    """
    target = as_index - 1
    if target < start or tokens[target].kind is not Kind.IDENT:
        return False
    if tokens[target].text == "self":
        # In a whole statement, bare `self` is invalid at the root and only
        # appears as `root::{self as name}`. A member span begins *after* its
        # opening brace, so `self` first there is the grouped-spelling root.
        return target > start or tokens[start - 1].text == "::"
    return _is_root_reference(tokens, start, target)


def _is_root_reference(tokens: tuple[Token, ...], start: int, index: int) -> bool:
    """Report whether the identifier at ``index`` is the statement's own root.

    A ``use`` statement reaches its root through a leading ``::`` or directly,
    and nothing else precedes it but the keyword itself. So the root is the only
    identifier between ``start`` and ``index``.
    """
    return all(tokens[cursor].kind is not Kind.IDENT for cursor in range(start, index))
