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

The helpers here answer the file-level questions: which crates a file imports,
and which leaf paths and member renames a statement holds. The smaller
questions about one statement - where it ends, whether it opens a group, what
it renames the crate to, which members its brace splits into - live in
`rust_prover_tools.trust_audit.use_paths`. `use_bindings` turns both sets of
facts into the local names an import binds, and
`rust_prover_tools.trust_audit.matcher` owns the vocabulary that decides which
of them matter.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_alias import crate_alias_indices
from rust_prover_tools.trust_audit.use_paths import (
    is_grouped,
    statement_end,
    top_level_members,
    use_root,
    use_roots,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "LOCAL_QUALIFIERS",
    "file_roots",
    "imported_roots",
    "member_aliases",
    "rooted_spans",
)

#: The path qualifiers that name a binding in this file rather than another
#: module: ``self`` for the current one, ``crate`` for the root, ``super`` for
#: the parent.
LOCAL_QUALIFIERS = frozenset({"self", "crate", "super"})

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
    if is_grouped(tokens, start, end):
        for member_start, member_end in top_level_members(tokens, start, end):
            yield from rooted_spans(tokens, member_start, member_end)
        return
    root = use_root(tokens, start, end)
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
