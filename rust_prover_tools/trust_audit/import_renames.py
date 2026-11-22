"""Decide whether a metavariable names an import's item or local alias.

``use std::$name as load;`` and ``use std::include as $name;`` choose at
expansion time which name an import binds, and the lexical scan never evaluates
the argument.  A later call under either spelling can therefore reach code the
audit never read, so both forms are refused.  A cast such as ``$value as u64``
is an expression rather than an import and stays accepted.
"""

from __future__ import annotations

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
    if index + 1 >= len(tokens) or tokens[index + 1].text != "as":
        return False
    return _inside_import(tokens, index)


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


def _opens_repetition(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``(`` at ``index`` opens a ``$( … )`` group."""
    return index >= 1 and tokens[index].text == "(" and tokens[index - 1].text == "$"


def _inside_import(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether ``index`` sits inside a ``use`` statement's path."""
    cursor = index - 1
    while cursor >= 0:
        text = tokens[cursor].text
        if text == "}":
            # A balanced brace group before the metavariable is a sibling
            # member of the same use tree; step over it.
            opener = _matching_opener(tokens, cursor)
            if opener is None:
                return False
            cursor = opener - 1
            continue
        if text == "{" and opens_use_group(tokens, cursor):
            cursor -= 1
            continue
        if _opens_repetition(tokens, cursor):
            # A use repetition wraps its members in ``$( … )``; the group is
            # part of the enclosing import, not a closing delimiter.
            cursor -= 2
            continue
        if text in {";", "(", ")", "[", "]", "{"}:
            return False
        if text == "use" and begins_import(tokens, cursor):
            return True
        cursor -= 1
    return False


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
