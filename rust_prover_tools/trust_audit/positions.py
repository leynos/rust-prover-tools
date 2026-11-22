"""Answer where a token sits, for the rules `context` applies.

The trust rules ask small position questions: is this word a bare use, a
member call, a path segment, or a module-qualified name?  Each answer is a
lexical fact about the file rather than state the scan accumulates, so the
predicates live here and the `Context` class reads them.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_bindings import member_bindings
from rust_prover_tools.trust_audit.use_paths import statement_end
from rust_prover_tools.trust_audit.use_tree import (
    LOCAL_QUALIFIERS,
    begins_import,
    rooted_spans,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# The ``::`` and the qualifier in front of a path's last segment.
_QUALIFIED_SEGMENTS = 2


def imports_root_glob(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
) -> bool:
    """Report whether the file glob-imports from one of the trust roots.

    The glob may sit in a module this file declares —
    ``mod m { pub use kani::*; }`` — so every ``use`` statement is read, not
    only one rooted at the file's own scope.
    """
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


def is_bare_use(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether rule 3 makes this token a bare use.

    A word after ``::`` belongs to a path and a word after ``.`` to a field or
    method; neither is the unqualified use rule 3 is about. Rule 1 has already
    claimed the ``kani::`` case, so only the remaining positions matter.
    """
    if not index:
        return True
    # A name after ``as`` is the local alias being defined, not a use of the
    # item: ``use kani::assume as any_where;`` must not report the borrowed
    # spelling as Kani's ``any_where``.
    return tokens[index - 1].text not in {"::", ".", "as"}


def member_names(
    tokens: tuple[Token, ...],
    roots: frozenset[str],
    words: frozenset[str],
) -> dict[str, frozenset[str]]:
    """Group every vocabulary member by the local name that denotes it.

    Two scopes may bind one local name to different items — ``use kani::assume
    as m;`` in one module and ``use kani::any_where as m;`` in another — and
    the scan is file-scoped, so it cannot tell which scope a later ``m(..)``
    sits in.  Keeping every item the name may denote, and reporting all of
    them, leaves the item for the reader to justify rather than dropping one.

    Only the selected vocabularies' words are grouped, so narrowing the
    selection narrows what a renamed import can report.
    """
    names: dict[str, set[str]] = {}
    for member, alias in member_bindings(tokens, roots):
        if member in words:
            names.setdefault(alias, set()).add(member)
    return {alias: frozenset(items) for alias, items in names.items()}


def is_local_qualified(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether this token is named through a local module qualifier.

    The qualifiers are ``self::``, ``crate::`` and ``super::``.  A named import
    binds its item in the module it appears in, so
    ``self::a(..)`` after ``use kani::assume as a;`` is the same call as the
    bare ``a(..)``.  ``crate::a`` names the same binding when the import sits
    at the crate root, and ``super::a`` names it from a child module; the scan
    is file-scoped, so it cannot rule either out and reports both.  Another
    module's name stays unreported, because the binding there is not shown.
    """
    return (
        index >= _QUALIFIED_SEGMENTS
        and tokens[index - 1].text == "::"
        and tokens[index - _QUALIFIED_SEGMENTS].text in LOCAL_QUALIFIERS
    )


def is_member_position(
    tokens: tuple[Token, ...],
    index: int,
    module_names: frozenset[str],
) -> bool:
    """Report whether a renamed member is used as the imported item here.

    Three positions count: the bare call, a ``self::``, ``crate::`` or
    ``super::`` qualifier that reaches the binding in this module, and a module
    this file declares, whose export the audit can read.
    """
    if is_bare_use(tokens, index) or is_local_qualified(tokens, index):
        return True
    return is_declared_module_qualified(tokens, index, module_names)


def is_declared_module_qualified(
    tokens: tuple[Token, ...],
    index: int,
    module_names: frozenset[str],
) -> bool:
    """Report whether a module this file declares qualifies the name.

    ``mod m { pub use kani::assume as a; } m::a(..)`` reaches the binding the
    module exports, and the import is in this file, so the audit can read it.
    The scan is file-scoped rather than scope-exact: an ``a`` imported in a
    different module would also be reported here, which is the fail-closed
    direction this audit takes everywhere else.
    """
    return (
        index >= _QUALIFIED_SEGMENTS
        and tokens[index - 1].text == "::"
        and tokens[index - _QUALIFIED_SEGMENTS].text in module_names
    )


def is_member_use(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether a renamed import is used as the item it renames.

    Only a call counts.  The name at its own definition follows ``as``, and a
    name in front of ``::`` qualifies a path rather than calling the item, so
    neither is a request for trust under another spelling.  A turbofish call
    such as ``a::<u32, _>(..)`` is the exception: Rust writes ``::`` before
    explicit generic arguments, so the ``::`` is part of the call.
    """
    if index and tokens[index - 1].text == "as":
        return False
    if index + 1 >= len(tokens) or tokens[index + 1].text != "::":
        return True
    return index + 2 < len(tokens) and tokens[index + 2].text == "<"


__all__ = (
    "imports_root_glob",
    "is_bare_use",
    "is_declared_module_qualified",
    "is_local_qualified",
    "is_member_position",
    "is_member_use",
    "member_names",
)
