"""Read which ``as`` in a ``use`` statement renames the crate itself.

A ``use`` statement can rename its root — ``use kani as k;`` — or one of its
members — ``use kani::{assume as a};``. The two mean different things to the
audit: a renamed crate is another spelling of the root, so a path through it is
still a trusted path, while a renamed member is one item under a local name, so
only a bare use of it is trust. Telling them apart is path arithmetic over the
statement's own tokens, so it lives here rather than in either caller.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("crate_alias_indices",)


def crate_alias_indices(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> cabc.Iterator[int]:
    """Yield the index of each ``as`` that renames the crate in one statement.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    start : int
        Token index of the first token of the statement or path.
    end : int
        Token index just past its last token.  The last ``as`` that can be
        reported is at ``end - 2``, because an alias identifier must follow it.

    Yields
    ------
    int
        The index of each ``as`` that renames the crate rather than a member.

    Notes
    -----
    At most one such ``as`` can appear in a statement Rust accepts, but the
    reader reports every one it finds rather than assuming: a statement it
    misreads is better reported twice than silently narrowed.

    Examples
    --------
    ``use kani as k;`` yields the index of its ``as``, while
    ``use kani::{assume as a};`` yields nothing.
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
