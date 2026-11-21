"""Read the paths one ``use`` statement names, however its braces nest.

A Rust use tree is a small language of its own.  ``use kani as k;`` names one
crate under an alias; ``use {std::mem, kani as k};`` names two, each with its
own alias; and ``use {{std::mem, kani as k}};`` is the same two imports one
brace deeper.  A member may itself be a group, and one crate may be renamed
more than once.

The helpers here answer what a statement's paths are: where it ends, whether
it opens a group, the root of one path, the names that rename the crate
itself, and the comma-separated members of a brace.  `use_tree` builds the
file-level questions on top of them, and
`rust_prover_tools.trust_audit.use_bindings` turns their answers into the
local names an import binds.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_alias import crate_alias_indices

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "is_grouped",
    "statement_end",
    "top_level_members",
    "use_aliases",
    "use_root",
    "use_roots",
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
    if is_grouped(tokens, start, end):
        return _grouped_roots(tokens, start, end)
    root = use_root(tokens, start, end)
    if root is None:
        return ()
    return tuple((root, alias) for alias in use_aliases(tokens, start, end)) or (
        (root, None),
    )


def use_root(tokens: tuple[Token, ...], start: int, end: int) -> str | None:
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
        Token index of the first token after ``use``.
    end : int
        Token index ending the path, which the caller already knows.  Reading
        it here rather than searching for the statement's ``;`` keeps a grouped
        import linear: every member would otherwise rescan the remainder of
        the statement.

    Returns
    -------
    str | None
        The first crate the statement names, or ``None`` when it names none.

    """
    for index in range(start, end):
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


def is_grouped(tokens: tuple[Token, ...], start: int, end: int) -> bool:
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
    for member_start, member_end in top_level_members(tokens, start, end):
        if is_grouped(tokens, member_start, member_end):
            roots.extend(_grouped_roots(tokens, member_start, member_end))
            continue
        root = use_root(tokens, member_start, member_end)
        if root is None:
            continue
        aliases = use_aliases(tokens, member_start, member_end)
        roots.extend((root, alias) for alias in aliases or (None,))
    return tuple(roots)


def top_level_members(
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
