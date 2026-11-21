"""Find the inline modules and ``#[path]`` attributes around a declaration.

A ``mod x;`` inside ``mod y { … }`` compiles ``y/x.rs``, not ``x.rs``, so the
enclosing inline chain travels with every module reference.  The same walk reads
the ``#[path]`` attribute attached to a declaration, including one a
``cfg_attr`` would select, which the audit refuses rather than guessing.

The chains are rebuilt on demand from parent links: a file with many nested
modules around many tokens would otherwise retain one tuple per token.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit.conditions import conditional_path
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.macro_rules import macro_rules
from rust_prover_tools.trust_audit.model import InlineModule
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

__all__ = ("InlineModules", "inline_module_names", "path_attribute")

# ``mod``, the module's name, and what follows it.
DECLARATION_TOKENS = 3
_INLINE = "{"


def inline_module_names(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return the names of the inline modules this file declares.

    A qualified path such as ``m::a`` names a local binding when ``m`` is an
    inline module in the same file.  A module declared elsewhere is not shown,
    so the scan cannot say what its names bind to and does not claim to.
    """
    rules = macro_rules(tokens)
    return InlineModules(tokens, rules.patterns).names


def names_module(token: Token, head: tuple[Token, ...]) -> bool:
    """Report whether ``token`` is ``mod`` and ``head`` names what follows it.

    ``head`` holds the module's name and the ``;`` or ``{`` after it. A short
    slice means the file ends inside a declaration, which names no file.
    """
    if token.kind is not Kind.IDENT or token.text != "mod":
        return False
    return len(head) == DECLARATION_TOKENS - 1 and head[0].kind is Kind.IDENT


def _opens_inline_module(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether the ``mod`` at ``index`` opens a body rather than a file."""
    head = tokens[index + 1 : index + DECLARATION_TOKENS]
    if not names_module(tokens[index], head):
        return False
    return head[1].text == _INLINE


class InlineModules:
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
                    path_attribute(tokens, spans, index),
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


def path_attribute(
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

    A bracket that is not an attribute ends the run.  It may belong to ordinary
    code before the declaration, such as an array type, and the ``#[path]``
    already found is still the one the declaration carries; returning nothing
    there would fall back to the bare search and read a different file.
    """
    index = skip_visibility(tokens, mod_index - 1)
    value: str | None = None
    while index >= 0:
        if tokens[index].text != "]":
            break
        opener = opening_bracket(tokens, index)
        if opener is None or opener not in spans:
            break
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
