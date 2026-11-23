"""Recognise the builtin ``stringify!`` and the spans it renders as text.

``stringify!`` returns its argument's spelling and compiles none of it, so a
declaration or invocation spelled there names nothing.  The name is only the
builtin when the file neither defines nor imports it and no glob import could
bind it: a user macro under that name may emit its argument as code.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.structure import matching
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import begins_import, rooted_spans

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("stringify_arguments",)

# The delimiters a macro invocation's body may open.
_MACRO_DELIMITERS = {"(": ")", "[": "]", "{": "}"}


def stringify_arguments(
    tokens: tuple[Token, ...],
    *,
    defined: cabc.Container[str] = frozenset(),
) -> frozenset[int]:
    """Return the token indices inside every ``stringify!`` argument group.

    ``stringify!`` returns its argument's spelling and compiles none of it, so
    a declaration or invocation written there names nothing.  The argument is
    skipped by its delimiters rather than by matching each line.  A file that
    defines its own ``stringify`` macro compiles the argument, so there is no
    exemption for it, and a glob import can bind a user macro under that name,
    so the file is refused rather than exempted.
    """
    if "stringify" in defined or _imports_stringify(tokens):
        return frozenset()
    glob = _imports_glob(tokens)
    skipped: set[int] = set()
    for index, token in enumerate(tokens[:-2]):
        if token.kind is not Kind.IDENT or token.text != "stringify":
            continue
        if not _is_builtin_stringify(tokens, index):
            continue
        if tokens[index + 1].text != "!":
            continue
        if glob:
            msg = (
                f"the stringify! at line {token.line}, column {token.column} "
                "cannot be read: a glob import in this file may bind the name "
                "stringify to a macro that compiles its argument"
            )
            raise TrustAuditError(msg)
        opening = index + 2
        closer = _MACRO_DELIMITERS.get(tokens[opening].text)
        if closer is None:
            continue
        closing = matching(tokens, opening, tokens[opening].text, closer)
        skipped.update(range(opening, closing + 1))
    return frozenset(skipped)


def _is_builtin_stringify(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``stringify`` at ``index`` names the builtin macro.

    A qualified path other than ``std::stringify`` or ``core::stringify`` can
    name a user macro that emits its argument, so it earns no exemption.  The
    qualifier must also begin the path: ``custom::std::stringify`` names a
    ``std`` module inside ``custom``, not the standard library's macro.
    """
    if index < 1 or tokens[index - 1].text != "::":
        return True
    qualifier = index - 2
    if qualifier < 0 or tokens[qualifier].text not in {"std", "core"}:
        return False
    separator = qualifier - 1
    if separator < 0 or tokens[separator].text != "::":
        # ``std::stringify`` with nothing before it: the path starts at std.
        return not _shadows_builtin_root(tokens, tokens[qualifier].text)
    head = separator - 1
    if head < 0:
        # A leading ``::std::stringify``: the path starts at std.
        return not _shadows_builtin_root(tokens, tokens[qualifier].text)
    if tokens[head].kind is Kind.IDENT or tokens[head].text == ">":
        return False
    return not _shadows_builtin_root(tokens, tokens[qualifier].text)


def _shadows_builtin_root(tokens: tuple[Token, ...], name: str) -> bool:
    """Report whether a local binding can shadow the ``std`` or ``core`` path.

    A module, an import of the same name or a rename to it makes a qualified
    ``std::stringify`` name that local item instead of the standard library.
    """
    return any(
        _item_shadows(tokens, index, token, name)
        for index, token in enumerate(tokens)
        if token.kind is Kind.IDENT and not token.raw
    )


def _item_shadows(
    tokens: tuple[Token, ...],
    index: int,
    token: Token,
    name: str,
) -> bool:
    """Report whether one item at ``index`` binds ``name`` locally."""
    if token.text == "mod":
        return _next_text(tokens, index + 1) == name
    if token.text == "extern":
        return (
            _next_text(tokens, index + 1) == "crate"
            and _next_text(tokens, index + 3) == name
        )
    if token.text == "use" and begins_import(tokens, index):
        return _import_shadows(tokens, index, name)
    return False


def _next_text(tokens: tuple[Token, ...], index: int) -> str | None:
    """Return the text at ``index``, or ``None`` past the end."""
    return tokens[index].text if index < len(tokens) else None


def _import_shadows(tokens: tuple[Token, ...], index: int, name: str) -> bool:
    """Report whether the import at ``index`` binds ``name`` locally."""
    end = statement_end(tokens, index + 1)
    for _root, alias in use_roots(tokens, index + 1, end):
        if alias == name:
            return True
    for root, start, stop in rooted_spans(tokens, index + 1, end):
        if root == name:
            continue
        idents = [
            item.text
            for item in tokens[start:stop]
            if item.kind is Kind.IDENT and not item.raw
        ]
        if idents and idents[-1] == name:
            return True
    return False


def _imports_stringify(tokens: tuple[Token, ...]) -> bool:
    """Report whether a ``use`` statement binds the name ``stringify``."""
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or token.raw:
            continue
        if not begins_import(tokens, index):
            continue
        for cursor in range(index + 1, len(tokens)):
            if tokens[cursor].text == ";":
                break
            if tokens[cursor].text == "stringify":
                return True
    return False


def _imports_glob(tokens: tuple[Token, ...]) -> bool:
    """Report whether a ``use`` statement imports a glob.

    A glob can bind any name its source exports, including ``stringify``, so a
    file containing one cannot prove which macro a bare ``stringify!`` names.
    """
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or token.raw:
            continue
        if not begins_import(tokens, index):
            continue
        for cursor in range(index + 1, len(tokens)):
            if tokens[cursor].text == ";":
                break
            if tokens[cursor].text == "*":
                return True
    return False
