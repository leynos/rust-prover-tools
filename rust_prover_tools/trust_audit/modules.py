"""Report the module files one Rust source compiles.

A ``mod x { … }`` body is already in this file, so only ``mod x;`` names a file
elsewhere. Its ``#[path]`` attribute, when present, decides which file that is;
without one the caller searches by Rust's rules. Both are surfaced rather than
resolved here, because resolution needs a repository root this module does not
have.

An inline module still contributes a *directory*, because a ``mod x;`` inside
it compiles ``…/x.rs`` relative to the module path, not to the declaring file's
own directory. The enclosing inline names therefore travel with the reference.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.conditions import conditional_path
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.macro_rules import MacroRules, macro_rules
from rust_prover_tools.trust_audit.model import InlineModule, ModuleReference
from rust_prover_tools.trust_audit.structure import (
    attribute_spans,
    matching,
    opening_bracket,
    path_value,
    previous_closer,
    skip_visibility,
)
from rust_prover_tools.trust_audit.syntax import Kind, Token

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("inline_module_names", "module_references")

# ``mod``, the module's name, and what follows it: a ``;`` for an external
# module or a ``{`` for an inline one.
_DECLARATION_TOKENS = 3
_INLINE = "{"
_EXTERNAL = ";"
# ``macro_rules``, ``!``, the macro's name and its body.


def module_references(tokens: tuple[Token, ...]) -> tuple[ModuleReference, ...]:
    """Collect the module files this source compiles, in source order.

    Parameters
    ----------
    tokens : tuple[Token, ...]
        The lexed source.

    Returns
    -------
    tuple[ModuleReference, ...]
        One reference per ``mod name;`` declaration, each carrying its name, the
        ``#[path]`` value it was given when it has one, its position, and the
        inline modules that enclose it.

    Raises
    ------
    TrustAuditError
        If a delimiter group never closes, so the declarations after it cannot
        be located; if a ``macro_rules!`` expansion declares a module named by
        a metavariable, so the file it compiles cannot be known; or if a
        declaration's ``#[path]`` is chosen by a ``cfg_attr``.

    Notes
    -----
    A ``macro_rules!`` pattern is source syntax rather than a declaration: a
    matcher may spell ``mod sample;`` as the input it matches without ever
    compiling that file.  Those tokens are skipped so the walk does not demand
    a file the crate never loads.

    An expansion is attributed to the invocations of its macro.  rustc
    resolves a ``mod`` declaration an expansion emits from the *invocation's*
    module directory, not from the definition's, so a macro defined at the
    crate root and invoked inside ``mod nested { … }`` compiles
    ``nested/child.rs``.  The expansion's own inline modules are appended to
    the chain at each invocation.  An invocation in another file is outside a
    per-file scan; that limitation is recorded in the package documentation.
    """
    references = []
    spans = dict(attribute_spans(tokens))
    rules = macro_rules(tokens)
    _refuse_metavariable_modules(tokens, rules.expansions)
    inline = _InlineModules(tokens, rules.patterns)
    for index, token in enumerate(tokens):
        head = tokens[index + 1 : index + _DECLARATION_TOKENS]
        if index in rules.patterns:
            continue
        if not _names_module(token, head) or head[1].text != _EXTERNAL:
            continue
        name = head[0]
        path = _path_attribute(tokens, spans, index)
        if index in rules.expansions:
            references.extend(
                _expanded_references(
                    rules,
                    inline,
                    index,
                    ModuleReference(
                        name.text,
                        path,
                        name.line,
                        name.column,
                        (),
                    ),
                ),
            )
            continue
        references.append(
            ModuleReference(
                name.text,
                path,
                name.line,
                name.column,
                inline.enclosing(index),
            ),
        )
    return tuple(references)


def inline_module_names(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return the names of the inline modules this file declares.

    A qualified path such as ``m::a`` names a local binding when ``m`` is an
    inline module in the same file.  A module declared elsewhere is not shown,
    so the scan cannot say what its names bind to and does not claim to.
    """
    rules = macro_rules(tokens)
    return _InlineModules(tokens, rules.patterns).names


def _expanded_references(
    rules: MacroRules,
    inline: _InlineModules,
    token_index: int,
    reference: ModuleReference,
) -> tuple[ModuleReference, ...]:
    """Attribute one expansion's declaration to each invocation in this file.

    The chain at a definition's own position says nothing about where the
    expansion compiles.  The chain at each invocation is the one rustc uses,
    and the inline modules the expansion itself declares are appended to it.
    A macro no invocation in this file names compiles nothing here, so the
    reference is dropped.
    """
    name = rules.owners[token_index]
    head = rules.heads[name]
    definition_chain = inline.enclosing(head)
    local_chain = inline.enclosing(token_index)[len(definition_chain) :]
    return tuple(
        dc.replace(
            reference,
            enclosing=(*inline.enclosing(site), *local_chain),
        )
        for site in rules.invocations.get(name, ())
    )


def _refuse_metavariable_modules(
    tokens: tuple[Token, ...],
    expansions: cabc.Iterable[int],
) -> None:
    """Fail closed on a ``mod $name;`` a macro expansion emits.

    The declaration's directory comes from the invocation, as any expansion's
    does, but its name is whatever the invocation's arguments put in the
    metavariable, and the lexical scan never evaluates those.  The audit
    therefore cannot tell which file the crate compiles, and reporting a clean
    scan would leave that file unread.  A literal ``mod name;`` in an expansion
    is left alone: the expansion spells the file itself, and module discovery
    already records it.  Only the expansion after ``=>`` is examined, so a
    matcher that spells ``mod $name;`` as the input it matches is not a
    declaration.
    """
    for index in sorted(expansions):
        token = tokens[index]
        next_index = index + 1
        if token.kind is not Kind.IDENT or token.text != "mod":
            continue
        if next_index < len(tokens) and tokens[next_index].text == "$":
            msg = (
                f"the macro_rules! expansion at line {token.line}, column "
                f"{token.column} declares a module named by a metavariable, "
                "so the audit cannot tell which file it compiles"
            )
            raise TrustAuditError(msg)


def _names_module(token: Token, head: tuple[Token, ...]) -> bool:
    """Report whether ``token`` is ``mod`` and ``head`` names what follows it.

    ``head`` holds the module's name and the ``;`` or ``{`` after it. A short
    slice means the file ends inside a declaration, which names no file.
    """
    if token.kind is not Kind.IDENT or token.text != "mod":
        return False
    return len(head) == _DECLARATION_TOKENS - 1 and head[0].kind is Kind.IDENT


def _opens_inline_module(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``mod`` at ``index`` opens a body rather than a file."""
    head = tokens[index + 1 : index + _DECLARATION_TOKENS]
    if not _names_module(tokens[index], head):
        return False
    return head[1].text == _INLINE


class _InlineModules:
    """Report the inline ``mod`` names that enclose a token.

    A single left-to-right walk pairs every ``mod name {`` with the ``}`` that
    closes it.  The chain at any index is rebuilt on demand from parent links
    rather than copied there: a file with ``D`` nested modules around ``N``
    ordinary tokens would otherwise retain ``N x D`` references even when the
    body declares no external module at all.
    """

    def __init__(
        self,
        tokens: tuple[Token, ...],
        patterns: cabc.Container[int] = frozenset(),
    ) -> None:
        """Pair every inline module's braces and record each token's chain."""
        # Each entry is (module, index of the enclosing entry, closing brace).
        self._modules: list[tuple[InlineModule, int | None, int]] = []
        self._innermost: list[int | None] = [None] * len(tokens)
        stack: list[int] = []
        spans = dict(attribute_spans(tokens))
        for index in range(len(tokens)):
            # A closing brace belongs to the module that *holds* the inline one,
            # so it is popped before the chain is recorded for that position.
            if stack and index == self._modules[stack[-1]][2]:
                stack.pop()
            self._innermost[index] = stack[-1] if stack else None
            if index not in patterns and _opens_inline_module(tokens, index):
                module = InlineModule(
                    tokens[index + 1].text,
                    _path_attribute(tokens, spans, index),
                )
                parent = stack[-1] if stack else None
                self._modules.append(
                    (module, parent, matching(tokens, index + 2, "{", "}")),
                )
                stack.append(len(self._modules) - 1)

    @property
    def names(self) -> frozenset[str]:
        """Every inline module name this file declares."""
        return frozenset(module.name for module, _, _ in self._modules)

    def enclosing(self, index: int) -> tuple[InlineModule, ...]:
        """Return the inline modules enclosing the token at ``index``.

        The chain is rebuilt from parent links, innermost first, and then
        reversed so a caller reads it outermost first.
        """
        chain: list[InlineModule] = []
        current = self._innermost[index]
        while current is not None:
            module, parent, _ = self._modules[current]
            chain.append(module)
            current = parent
        chain.reverse()
        return tuple(chain)


def _path_attribute(
    tokens: tuple[Token, ...],
    spans: cabc.Mapping[int, int],
    mod_index: int,
) -> str | None:
    """Return the ``#[path]`` value attached to the item at ``mod_index``.

    The attribute list is the run of ``#[…]`` spans immediately before the
    declaration, but a visibility modifier may sit between the two:
    ``#[path = "x.rs"] pub(crate) mod x;`` attaches the attribute to the
    declaration all the same. So the walk starts by stepping over any
    ``pub``/``pub(…)`` and only then looks for ``]``.

    Every attribute in the run applies to the declaration, so the walk continues
    past an attribute that is not ``#[path]``. Missing that step is not a
    refusal a caller can notice: the module then falls back to Rust's bare
    ``mod x;`` search, which reads a different file from the one the crate
    compiles and can report a clean audit of code that is not in the proof.

    The whole run is examined even after a direct ``#[path]`` is found, because
    a ``cfg_attr`` beside it can still carry one. A ``cfg_attr`` that carries a
    ``path`` is refused rather than searched for: whether the attribute applies
    depends on the configuration the crate is built with, and the audit reads
    one tree.

    When the declaration carries more than one direct ``#[path]``, the first in
    source order decides, which is the attribute ``rustc`` compiles; the walk
    runs backwards, so each find overwrites the previous one.
    """
    index = skip_visibility(tokens, mod_index - 1)
    value: str | None = None
    while index >= 0:
        if tokens[index].text != "]":
            return None
        opener = opening_bracket(tokens, index)
        if opener is None or opener not in spans:
            return None
        found = path_value(tokens, opener, index)
        if found is not None:
            value = found
        if conditional_path(tokens, opener, index):
            _refuse_conditional_path(tokens, mod_index)
        index = previous_closer(tokens, opener)
    return value


def _refuse_conditional_path(tokens: tuple[Token, ...], mod_index: int) -> None:
    """Fail closed on a module whose file a configuration decides."""
    name = tokens[mod_index + 1]
    msg = (
        f"the module {name.text!r} at line {name.line}, column {name.column} "
        "carries a conditional #[path], so which file it compiles depends on "
        "the configuration"
    )
    raise TrustAuditError(msg)
