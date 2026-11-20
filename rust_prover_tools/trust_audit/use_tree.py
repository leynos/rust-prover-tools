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
its aliases and the member renames inside it. `use_bindings` turns those facts
into the local names an import binds, and
`rust_prover_tools.trust_audit.matcher` owns the vocabulary that decides which
of them matter.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_alias import crate_alias_indices

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "file_roots",
    "imported_roots",
    "member_aliases",
    "rooted_spans",
    "statement_end",
    "use_aliases",
    "use_root",
    "use_roots",
)

# ``extern``, ``crate``: the two words that open a crate declaration.
_EXTERN_CRATE_KEYWORDS = ("extern", "crate")


def imported_roots(
    tokens: tuple[Token, ...],
    roots: cabc.Set[str],
) -> frozenset[str]:
    """Return the roots in ``roots`` this file imports.

    Every root of every statement counts, not just its leading word.
    ``use {std::mem, kani};`` imports ``kani`` as surely as the ungrouped form
    does, and reading only the first identifier would quietly switch the
    matcher's bare-word rule off for that file.

    ``extern crate kani;`` is the same import spelled without a ``use``, and it
    likewise makes a bare ``assume`` resolve to Kani, so it counts too. The
    caller supplies the roots it cares about, which keeps this module free of
    the vocabulary that names them.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    roots : Set[str]
        The crate names the caller cares about.

    Returns
    -------
    frozenset[str]
        The subset of ``roots`` the file imports.

    """
    return frozenset(file_roots(tokens) & roots)


def file_roots(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return every crate the file's ``use`` statements and ``extern crate`` name.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.

    Returns
    -------
    frozenset[str]
        Every crate the file names as an import root, in any statement.

    """
    found = set()
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT:
            continue
        declared = _extern_crate_root(tokens, index)
        if declared is not None:
            found.add(declared)
        elif token.text == "use":
            end = statement_end(tokens, index + 1)
            found.update(root for root, _ in use_roots(tokens, index + 1, end))
    return frozenset(found)


def _extern_crate_root(tokens: tuple[Token, ...], index: int) -> str | None:
    """Return the crate an ``extern crate`` declaration at ``index`` names."""
    head = tuple(item.text for item in tokens[index : index + 2])
    if head != _EXTERN_CRATE_KEYWORDS:
        return None
    name_index = index + len(_EXTERN_CRATE_KEYWORDS)
    if name_index >= len(tokens):
        return None
    name = tokens[name_index]
    return name.text if name.kind is Kind.IDENT else None


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

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token after ``use``.
    end : int
        Token index ending the statement: the closing ``;`` for a whole
        statement, or the brace or comma ending a member span.

    Returns
    -------
    tuple[tuple[str, str | None], ...]
        One ``(crate, alias)`` pair per rooted path, with ``None`` as the
        alias when the path does not rename its crate.

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

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token after ``use``.  No ``end`` is taken:
        the walk stops at the next ``;``.

    Returns
    -------
    str | None
        The first crate the statement names, or ``None`` when it names none.

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
    statement's own alias: matching every ``a::`` path against the trust root
    would claim what the source does not.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token after ``use``.
    end : int
        Token index ending the statement: its closing ``;``, or the brace or
        comma ending a member span.

    Returns
    -------
    frozenset[str]
        The names bound to the crate itself.

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
    """Return the index of the ``;`` closing the statement opened at ``start``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index to search from.

    Returns
    -------
    int
        The index of the next ``;`` at or after ``start``, or ``len(tokens)``
        when the source has none.

    """
    for index in range(start, len(tokens)):
        if tokens[index].text == ";":
            return index
    return len(tokens)


def rooted_spans(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[str, int, int]]:
    """Yield ``(root, start, end)`` for each leaf path of one ``use`` statement.

    A brace holds one path per member, each of which may name its own crate, so
    each member is reported with the root governing *it* rather than with the
    first root in the statement: ``use {std::x as X, kani::assume as a};``
    renames ``a`` out of Kani and ``X`` out of the standard library.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token after ``use``.
    end : int
        Token index ending the statement: its closing ``;``, or the brace or
        comma ending a member span.

    Returns
    -------
    Iterator[tuple[str, int, int]]
        ``(root, start, end)`` for each leaf path, where the two indices bound
        the tokens of that path.

    """
    if _is_grouped(tokens, start, end):
        for member_start, member_end in _top_level_members(tokens, start, end):
            yield from rooted_spans(tokens, member_start, member_end)
        return
    root = use_root(tokens, start)
    if root is not None:
        yield root, start, end


def member_aliases(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[tuple[str, str]]:
    """Yield ``(item, local name)`` for each member rename in one rooted path.

    The crate's own rename is left out. A member rename is any other ``as``
    with an identifier on each side, so ``kani::foo::assume as a`` and
    ``kani::{assume as a, assert as b}`` are read alike.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token of one rooted path.
    end : int
        Token index just past that path's last token: the brace or comma that
        ends a member span, or the statement's closing ``;``.

    Returns
    -------
    Iterator[tuple[str, str]]
        ``(item, local name)`` for each member rename in the path.

    """
    crate_aliases = set(crate_alias_indices(tokens, start, end))
    for index in range(start + 1, end - 1):
        if index in crate_aliases or tokens[index].text != "as":
            continue
        member, alias = tokens[index - 1], tokens[index + 1]
        if member.kind is Kind.IDENT and alias.kind is Kind.IDENT:
            yield member.text, alias.text


def _is_grouped(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether the statement, after any leading ``::``, opens a brace."""
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
