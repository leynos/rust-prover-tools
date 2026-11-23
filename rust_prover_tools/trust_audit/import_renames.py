"""Decide whether a metavariable names an import's item or local alias.

``use std::$name as load;`` and ``use std::include as $name;`` choose at
expansion time which name an import binds, and the lexical scan never evaluates
the argument.  A later call under either spelling can therefore reach code the
audit never read, so both forms are refused.  A cast such as ``$value as u64``
is an expression rather than an import and stays accepted.
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.structure import opening_paren
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import opens_use_group
from rust_prover_tools.trust_audit.use_tree import begins_import

__all__ = ("is_import_destination", "is_import_rename")

# The metavariable marker, ``as`` and the item before it.
_IMPORT_ALIAS_SEGMENTS = 3


def is_import_rename(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether a metavariable is the item a ``use`` rename binds.

    ``use std::$name as load;`` chooses at expansion time which opaque macro
    ``load!`` calls, so the scan must refuse rather than accept the file.  A
    cast such as ``$value as u64`` is an expression, not an import: the search
    stops at the statement or delimiter that surrounds it.
    """
    if index + 1 < len(tokens) and tokens[index + 1].text == "as":
        return _inside_import(tokens, index)
    rename = _as_after_repetition(tokens, index)
    return rename is not None and _inside_import(tokens, rename)


def is_import_destination(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether a metavariable is the local name a ``use`` alias binds.

    ``use std::include as $name;`` chooses at expansion time what the local
    name is, and a later call under that name is a trust use the scan cannot
    see.  A cast such as ``$value as u64`` has no metavariable after ``as``,
    and a cast to a metavariable type is outside any import statement, so both
    remain accepted.
    """
    if index < _IMPORT_ALIAS_SEGMENTS or tokens[index - 1].text != "$":
        return False
    if tokens[index - 2].text != "as":
        return False
    if tokens[index - _IMPORT_ALIAS_SEGMENTS].kind is not Kind.IDENT:
        return False
    return _inside_import(tokens, index)


def _as_after_repetition(tokens: tuple[Token, ...], index: int) -> int | None:
    """Return the ``as`` after a repetition ending at the metavariable.

    ``use std::{$($name)* as load};`` renames the repeated item.  The token
    after the metavariable is the repetition's ``)``, so the ordinary
    next-token test misses the rename; the operator and ``as`` follow it.
    """
    close = index + 1
    if close >= len(tokens) or tokens[close].text != ")":
        return None
    opener = opening_paren(tokens, close)
    if opener is None or not _opens_repetition(tokens, opener):
        return None
    following = close + 1
    for offset in range(0, 4):
        operator = following + offset
        if operator >= len(tokens):
            return None
        if tokens[operator].text in {"*", "+", "?"}:
            after = operator + 1
            return after if after < len(tokens) and tokens[after].text == "as" else None
    return None


def _opens_repetition(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``(`` at ``index`` opens a ``$( … )`` group."""
    return index >= 1 and tokens[index].text == "(" and tokens[index - 1].text == "$"


#: Tokens that end an expression rather than continue an import.
_END_TOKENS = frozenset({";", "(", "[", "]", "{"})


def _inside_import(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether ``index`` sits inside a ``use`` statement's path."""
    cursor = index - 1
    while cursor >= 0:
        cursor, verdict = _step_back(tokens, cursor)
        if verdict is not None:
            return verdict
    return False


def _step_back(
    tokens: tuple[Token, ...],
    cursor: int,
) -> tuple[int, bool | None]:
    """Return the next cursor and, when the walk is decided, its verdict.

    A balanced sibling group belongs to the same use tree and is stepped over.
    An ordinary ``)`` ends the walk: a cast such as ``$value as u64`` is not an
    import, and continuing past it could reach a ``use`` elsewhere in the file.
    """
    text = tokens[cursor].text
    if text in {")", "}"}:
        skipped = (
            _before_repetition(tokens, cursor)
            if text == ")"
            else _before_brace_group(tokens, cursor)
        )
        return (-1, False) if skipped is None else (skipped, None)
    if text == "{" and opens_use_group(tokens, cursor):
        return cursor - 1, None
    if _opens_repetition(tokens, cursor):
        # A use repetition wraps its members in ``$( … )``; the group is part
        # of the enclosing import, not a closing delimiter.
        return cursor - 2, None
    if text == "use" and begins_import(tokens, cursor):
        return -1, True
    if text in _END_TOKENS:
        return -1, False
    return cursor - 1, None


def _before_brace_group(tokens: tuple[Token, ...], close: int) -> int | None:
    """Return the index before the brace group closing at ``close``."""
    opener = _matching_opener(tokens, close)
    return None if opener is None else opener - 1


def _before_repetition(tokens: tuple[Token, ...], close: int) -> int | None:
    """Return the index before a ``$( … )`` group closing at ``close``.

    ``None`` means the ``)`` closes an ordinary expression, which ends the
    search: a cast such as ``$value as u64`` is not an import.
    """
    opener = opening_paren(tokens, close)
    if opener is None or not _opens_repetition(tokens, opener):
        return None
    # The repetition is a sibling member of the enclosing use tree, so the walk
    # resumes before its introducing ``$``.
    return opener - 2


def _matching_opener(tokens: tuple[Token, ...], close: int) -> int | None:
    """Return the ``{`` matching the ``}`` at ``close``, if it balances."""
    depth = 0
    for index in range(close, -1, -1):
        if tokens[index].text == "}":
            depth += 1
        elif tokens[index].text == "{":
            depth -= 1
            if depth == 0:
                return index
    return None
