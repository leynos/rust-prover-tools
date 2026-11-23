"""Derive the local names a file's imports bind to trusted items.

The matcher asks two questions of a file's imports: *which local names stand
for a trust root*, and *which stand for a member of one*. Both answers are the
least fixpoint of the import statements, because an alias may itself be renamed
before it is used, so they are computed here rather than alongside the
vocabulary that decides which roots and members matter.

`alias_bindings` reports crate aliases, which keep their qualified-path
meaning; `member_bindings` reports member aliases, which are trust only when
used bare. Reading the statements once would miss a second rename and report
the file as trusting nothing at that use.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.crate_bindings import (
    alias_bindings,
)
from rust_prover_tools.trust_audit.crate_bindings import (
    merge as _merge,
)
from rust_prover_tools.trust_audit.member_paths import Scope, span_bindings
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end
from rust_prover_tools.trust_audit.use_tree import (
    begins_import,
    imports_root_glob,
    rooted_spans,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("alias_bindings", "member_bindings")


def member_bindings(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
) -> frozenset[tuple[str, str]]:
    """Return ``(item, local name)`` for each renamed import from ``roots``.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.
    roots : Container[str]
        The crate names whose members are followed, including any local name
        the file binds one of them to.

    Returns
    -------
    frozenset[tuple[str, str]]
        One ``(item, local name)`` pair per binding, where ``item`` is the
        member's own name and ``local name`` is the spelling the file uses for
        it.  Every pair a rename can reach is included, so one local name may
        appear in several pairs.

    Notes
    -----
    ``use kani::{assume as a};`` binds ``a`` to Kani's ``assume``, so a later
    ``a(x)`` is the same request for trust under another spelling. Unlike a
    crate alias, the local name stands for one item: only a bare use of it is
    trust, and a path through it is not, because a member is not a crate.

    A rename of the crate itself — ``use kani as k;`` or
    ``use kani::{self as k};`` — is not a member binding, and ``self`` is not an
    item the vocabulary names. `alias_bindings` reports those instead.

    A member can be renamed again: after ``use kani::{assume as a};``, the
    statement ``use a as b;`` binds ``b`` to the same item. The search
    therefore repeats until a pass adds no pair, just as `alias_bindings` does
    for crates; reading the statements once would miss ``b(x)`` and report the
    file as trusting nothing at that use.

    One local name may stand for several items. Rust scopes allow ``use
    kani::assume as m;`` in one module and ``use kani::any_where as m;`` in
    another, and the scan is file-scoped, so it reports every item ``m`` may
    denote rather than keeping whichever scope happened to be read last.

    Examples
    --------
    ``use kani::{assume as a};`` reports ``(('assume', 'a'),)``.
    """
    bindings: dict[str, set[str]] = {}
    root_glob = imports_root_glob(tokens, roots)
    while True:
        # The pairs already explained, so a round that only re-reports them
        # ends the search.  Extending with the rest would leave the loop with
        # work to do forever.
        known = {(alias, item) for alias, items in bindings.items() for item in items}
        discovered = _binding_round(tokens, roots, bindings, root_glob=root_glob)
        fresh = {
            (alias, item)
            for alias, items in discovered.items()
            for item in items
            if (alias, item) not in known
        }
        if not fresh:
            return frozenset((item, alias) for alias, item in known)
        _merge(bindings, discovered)


def _binding_round(
    tokens: tuple[Token, ...],
    roots: cabc.Container[str],
    bindings: cabc.Mapping[str, cabc.Iterable[str]],
    *,
    root_glob: bool,
) -> dict[str, set[str]]:
    """Return the names one pass binds to items, keyed by the local name.

    A path rooted at one of ``roots`` names a member of that crate. A path
    rooted at a name ``bindings`` already maps to items is a further rename of
    those same items, which Rust permits as ``use alias as another;``.  The
    file-scoped glob fact is computed once by the caller and carried in
    ``Scope``.
    """
    discovered: dict[str, set[str]] = {}
    scope = Scope(roots, bindings, root_glob)
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        for span in rooted_spans(tokens, index + 1, end):
            _merge(discovered, span_bindings(tokens, span, scope))
    return discovered
