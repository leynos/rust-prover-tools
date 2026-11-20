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

import typing as typ

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

__all__ = ("module_references",)

# ``mod``, the module's name, and what follows it: a ``;`` for an external
# module or a ``{`` for an inline one.
_DECLARATION_TOKENS = 3
_INLINE = "{"
_EXTERNAL = ";"


def module_references(tokens: tuple[Token, ...]) -> tuple[ModuleReference, ...]:
    """Collect the module files this source compiles, in source order."""
    references = []
    spans = dict(attribute_spans(tokens))
    inline = _InlineModules(tokens)
    for index, token in enumerate(tokens):
        head = tokens[index + 1 : index + _DECLARATION_TOKENS]
        if not _names_module(token, head) or head[1].text != _EXTERNAL:
            continue
        name = head[0]
        references.append(
            ModuleReference(
                name.text,
                _path_attribute(tokens, spans, index),
                name.line,
                name.column,
                inline.enclosing(index),
            ),
        )
    return tuple(references)


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
    closes it, so the enclosing chain at any index is the stack the walk holds
    there. Building that once per file keeps the lookup from re-scanning the
    token stream for every ``mod`` declaration.
    """

    def __init__(self, tokens: tuple[Token, ...]) -> None:
        """Pair every inline module's braces and record each token's chain."""
        self._chains: list[tuple[InlineModule, ...]] = [()] * len(tokens)
        stack: list[tuple[InlineModule, int]] = []
        spans = dict(attribute_spans(tokens))
        for index in range(len(tokens)):
            # A closing brace belongs to the module that *holds* the inline one,
            # so it is popped before the chain is recorded for that position.
            if stack and index == stack[-1][1]:
                stack.pop()
            self._chains[index] = tuple(module for module, _ in stack)
            if _opens_inline_module(tokens, index):
                module = InlineModule(
                    tokens[index + 1].text,
                    _path_attribute(tokens, spans, index),
                )
                stack.append((module, matching(tokens, index + 2, "{", "}")))

    def enclosing(self, index: int) -> tuple[InlineModule, ...]:
        """Return the inline modules enclosing the token at ``index``."""
        return self._chains[index]


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
    """
    index = skip_visibility(tokens, mod_index - 1)
    while index >= 0:
        if tokens[index].text != "]":
            return None
        opener = opening_bracket(tokens, index)
        if opener is None or opener not in spans:
            return None
        value = path_value(tokens, opener, index)
        if value is not None:
            return value
        index = previous_closer(tokens, opener)
    return None
