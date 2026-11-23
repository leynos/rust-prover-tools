"""Follow the names a ``use`` statement binds a crate to.

A crate may be renamed on import (``use kani as k;``), renamed again through a
local qualifier (``use self::k as kk;``), or renamed by ``extern crate``.  The
matcher treats every resulting name exactly like the literal crate root, so the
search here repeats until a pass adds no name.  Member bindings live in
`rust_prover_tools.trust_audit.use_bindings`.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.inline_modules import (
    declared_module_aliases,
    inline_module_names,
)
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.structure import matching
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import (
    statement_end,
    top_level_members,
    use_roots,
)
from rust_prover_tools.trust_audit.use_tree import (
    LOCAL_QUALIFIERS,
    begins_import,
    member_aliases,
    rooted_spans,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "merge")

# ``extern``, ``crate``, the crate's name, ``as``, and the local name.
_EXTERN_CRATE_TOKENS = 5


def merge(target: dict[str, set[str]], extra: dict[str, set[str]]) -> None:
    """Add every ``(name, item)`` pair in ``extra`` to ``target``."""
    for alias, items in extra.items():
        target.setdefault(alias, set()).update(items)


def alias_bindings(tokens: tuple[Token, ...], root: str) -> frozenset[str]:
    """Return every local name a file binds ``root`` to, plus ``root`` itself.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    root : str
        The crate name to follow, such as ``kani``.

    Returns
    -------
    frozenset[str]
        ``root`` and every name the file binds it to, including a name bound
        only through a rename of a rename.

    Notes
    -----
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

    A child module's ``use crate::k;`` names an item the crate root declares,
    and the per-file scan cannot see that declaration.  The local name is
    therefore treated as a possible crate alias, which is the fail-closed
    direction: the scan has no evidence that ``k::assume`` is *not* Kani.
    Over-reporting asks a reviewer to read one more line; staying silent would
    hide the assumption.

    Examples
    --------
    A file that renames the crate reports both names: ``use kani as k;`` binds
    ``k`` and ``kani``. An ``extern crate kani as k;`` reports the same pair.
    """
    names = {root} | _crate_root_imports(tokens)
    while True:
        discovered = _alias_round(tokens, names)
        if discovered <= names:
            return frozenset(names)
        names |= discovered


#: The qualifiers that name an item the scan cannot see: the crate root, the
#: current module, or its parent.  A child module's ``use super::k;`` names an
#: item declared in the parent, so the local name is a possible crate alias.
_ITEM_QUALIFIERS = frozenset({"crate", "self", "super"})


def _crate_root_imports(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return the local names a qualified item import binds in this file.

    An import from the crate root, the current module or a parent may name an
    alias the scan cannot see; its grouped form (``use crate::{k as kk, j};``)
    binds the same names.  ``super`` may repeat.  A deeper path names a member
    of a module outside the file, and its local name may still be that module's
    re-export, so the local spelling is treated as a possible alias too.  What
    a qualified use would carry is what the scan records.
    """
    names: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        for root, start, stop in rooted_spans(tokens, index + 1, end):
            if root in _ITEM_QUALIFIERS:
                names.update(_crate_root_names(tokens, start, stop))
    return frozenset(names)


def _crate_root_names(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
) -> set[str]:
    """Return the local names one qualified item path binds."""
    index = start
    while index < stop and tokens[index].text == "::":
        index += 1
    while (
        index + 1 < stop
        and tokens[index].text in _ITEM_QUALIFIERS
        and tokens[index + 1].text == "::"
    ):
        index += 2
    if index >= stop:
        return set()
    if tokens[index].text == "{":
        return _grouped_crate_names(tokens, index)
    return _crate_member_names(tokens, index, stop)


def _grouped_crate_names(tokens: tuple[Token, ...], opener: int) -> set[str]:
    """Return the local names a ``crate::{…}`` group binds."""
    return _nested_crate_names(tokens, opener)


def _nested_crate_names(tokens: tuple[Token, ...], opener: int) -> set[str]:
    """Return every local name the brace group at ``opener`` binds.

    A member may itself be a group or a deeper path ending in one, so each
    member is read by the same rules as the outermost list: a rename keeps its
    alias, a wildcard at any depth is refused, and a plain member keeps its own
    name.
    """
    closer = matching(tokens, opener, "{", "}")
    names: set[str] = set()
    for member_start, member_end in top_level_members(tokens, opener, closer + 1):
        names.update(_crate_member_names(tokens, member_start, member_end))
    return names


def _crate_member_names(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
) -> set[str]:
    """Return the local name one crate-root path binds, if it can be an alias.

    A deeper path such as ``crate::prelude::k as kk`` names a member of a
    module under the crate root.  The per-file scan cannot see that module's
    exports, so the local name may still be a crate re-export; it is treated as
    a possible alias, the same fail-closed direction as a single-segment
    import.  A path that binds nothing, such as a bare ``crate``, contributes
    no name.

    A path that ends in a brace group — ``prelude::{k, j}`` — binds one name
    per member, each of which may be renamed or be a wildcard.  Reading the
    whole span as one path would keep only its last identifier and lose the
    others, so the group is expanded first.
    """
    opener = next(
        (index for index in range(start, stop) if tokens[index].text == "{"),
        None,
    )
    if opener is not None:
        names = _nested_crate_names(tokens, opener)
        parent = _parent_name_for_self(tokens, start, opener)
        if parent is not None:
            names.add(parent)
        return names
    if start < stop and tokens[stop - 1].text == "*":
        # A wildcard imports names the per-file scan never sees, at any depth:
        # ``use crate::prelude::*`` exports whatever that module re-exports,
        # including a trust alias.  Dropping it would let a later
        # ``k::assume`` pass unreported.
        token = tokens[stop - 1]
        msg = (
            f"the wildcard import at line {token.line}, column {token.column} "
            "imports names the audit cannot determine"
        )
        raise TrustAuditError(msg)
    alias = _trailing_alias(tokens, start, stop)
    if alias is not None:
        return {alias}
    for index in range(stop - 1, start - 1, -1):
        token = tokens[index]
        if token.kind is not Kind.IDENT:
            continue
        return set() if token.text in LOCAL_QUALIFIERS else {token.text}
    return set()


def _parent_name_for_self(
    tokens: tuple[Token, ...],
    start: int,
    opener: int,
) -> str | None:
    """Return the parent name an unrenamed ``self`` member binds.

    A renamed ``self`` binds only its alias, which `_nested_crate_names`
    already reports.
    """
    if not _has_unrenamed_self(tokens, opener):
        return None
    for index in range(opener - 1, start - 1, -1):
        token = tokens[index]
        if token.kind is not Kind.IDENT:
            continue
        return None if token.text in LOCAL_QUALIFIERS else token.text
    return None


def _has_unrenamed_self(tokens: tuple[Token, ...], opener: int) -> bool:
    """Report whether a brace group holds a bare ``self`` member."""
    closer = matching(tokens, opener, "{", "}")
    return any(
        member_end - member_start == 1 and tokens[member_start].text == "self"
        for member_start, member_end in top_level_members(tokens, opener, closer + 1)
    )


def _trailing_alias(
    tokens: tuple[Token, ...],
    start: int,
    stop: int,
) -> str | None:
    """Return the local name an import's ``as`` binds, if it has one."""
    for index in range(stop - 2, start - 1, -1):
        if tokens[index].text != "as":
            continue
        alias = tokens[index + 1]
        return alias.text if alias.kind is Kind.IDENT else None
    return None


def _alias_round(
    tokens: tuple[Token, ...],
    names: cabc.Container[str],
) -> set[str]:
    """Return every name one pass binds to a crate already in ``names``."""
    discovered: set[str] = set()
    modules = _declared_modules(tokens)
    modules |= declared_module_aliases(tokens, modules)
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT:
            continue
        declared = _extern_crate_alias(tokens, index)
        if declared is not None:
            crate, alias = declared
            if crate in names:
                discovered.add(alias)
            continue
        if not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        discovered.update(
            alias
            for crate, alias in use_roots(tokens, index + 1, end)
            if crate in names and alias is not None
        )
        discovered.update(_requalified_aliases(tokens, index + 1, end, names))
        discovered.update(
            _module_path_aliases(
                tokens,
                index + 1,
                end,
                _AliasScope(modules),
            ),
        )
    return discovered


def _declared_modules(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return the modules this file declares, inline and external.

    A file cannot resolve another module's exports, but it can see that a
    module it declares re-exports a crate alias; that is the case this set
    covers.
    """
    return inline_module_names(tokens) | frozenset(
        reference.name for reference in module_references(tokens)
    )


@dc.dataclass(frozen=True, slots=True)
class _AliasScope:
    """The declared modules one pass reads a path through."""

    modules: cabc.Container[str]


def _module_path_aliases(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
    scope: _AliasScope,
) -> set[str]:
    """Return names bound through a module this file declares.

    ``mod m { pub use kani as k; } use m::k as kk;`` binds ``kk`` to the same
    crate.  The module's export is visible in this file, so the rename is
    followed; a module declared elsewhere cannot be resolved.  An import that
    names a member of a declared module (``use exports::k;`` where ``exports``
    is declared but not read here) binds a name that module may re-export as a
    crate alias, so the local name is treated as a possible alias — the same
    fail-closed policy as a deeper crate-root import.  A wildcard at any depth
    is refused because its bindings cannot be determined.
    """
    discovered: set[str] = set()
    for root, span_start, span_stop in rooted_spans(tokens, start, end):
        if root in scope.modules:
            discovered.update(_crate_member_names(tokens, span_start, span_stop))
    return discovered


def _requalified_aliases(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
    names: cabc.Container[str],
) -> set[str]:
    """Return crate aliases bound through ``self::``, ``crate::`` or ``super::``.

    ``use kani as k; use self::k as kk;`` binds ``kk`` to the same crate, so a
    later ``kk::assume`` is Kani's.  The qualifier names the module holding the
    binding, not a crate, and the member names the alias already in ``names``.
    The fixpoint in `alias_bindings` makes the order of the two statements
    irrelevant.
    """
    discovered: set[str] = set()
    for root, span_start, span_stop in rooted_spans(tokens, start, end):
        if root not in LOCAL_QUALIFIERS:
            continue
        discovered.update(
            alias
            for member, alias in member_aliases(tokens, span_start, span_stop)
            if member in names
        )
    return discovered


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
