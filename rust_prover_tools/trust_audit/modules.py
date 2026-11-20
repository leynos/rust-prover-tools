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

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.model import InlineModule, ModuleReference
from rust_prover_tools.trust_audit.structure import (
    attribute_spans,
    conditional_path,
    matching,
    opening_bracket,
    path_value,
    previous_closer,
    skip_visibility,
    texts,
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
# ``macro_rules``, ``!``, the macro's name and its body.
_MACRO_HEAD = ("macro_rules", "!")
_RULE_ARROW = "=>"
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}


def module_references(tokens: tuple[Token, ...]) -> tuple[ModuleReference, ...]:
    """Collect the module files this source compiles, in source order.

    A ``macro_rules!`` pattern is source syntax rather than a declaration: a
    matcher may spell ``mod sample;`` as the input it matches without ever
    compiling that file. Those tokens are skipped so the walk does not demand a
    file the crate never loads.
    """
    references = []
    spans = dict(attribute_spans(tokens))
    patterns, expansions = _macro_spans(tokens)
    _refuse_metavariable_modules(tokens, expansions)
    inline = _InlineModules(tokens, patterns)
    for index, token in enumerate(tokens):
        head = tokens[index + 1 : index + _DECLARATION_TOKENS]
        if index in patterns:
            continue
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


def _macro_spans(
    tokens: tuple[Token, ...],
) -> tuple[frozenset[int], frozenset[int]]:
    """Return the pattern and expansion tokens of every ``macro_rules!``.

    A rule is a pattern followed by ``=>`` and a body, so the group before each
    arrow is the pattern and the group after it is the expansion. Only the
    pattern is skipped: an expansion that emits ``mod x;`` is code the crate
    compiles, even though Rust resolves its file from the macro's own position.
    The expansion is returned separately so a declaration whose name is a
    metavariable can be refused rather than silently missed.
    """
    patterns: set[int] = set()
    expansions: set[int] = set()
    for index in range(len(tokens) - len(_MACRO_HEAD)):
        if texts(tokens, index, len(_MACRO_HEAD)) != _MACRO_HEAD:
            continue
        opening = index + len(_MACRO_HEAD) + 1
        closing = _group_end(tokens, opening, len(tokens))
        if closing is None:
            continue
        body_patterns, body_expansions = _rule_spans(tokens, opening, closing)
        patterns.update(body_patterns)
        expansions.update(body_expansions)
    return frozenset(patterns), frozenset(expansions)


def _rule_spans(
    tokens: tuple[Token, ...],
    opening: int,
    closing: int,
) -> tuple[set[int], set[int]]:
    """Return the pattern and expansion indices of each rule in a macro body.

    A group is a pattern when the token after it is ``=>``, and the group after
    the arrow is that rule's expansion. A rule whose arrow ends the body has no
    expansion to read.
    """
    patterns: set[int] = set()
    expansions: set[int] = set()
    index = opening + 1
    while index < closing:
        group = _group_end(tokens, index, closing)
        if group is None:
            index += 1
            continue
        if group + 1 < closing and tokens[group + 1].text == _RULE_ARROW:
            patterns.update(range(index, group + 1))
            expansion = _group_end(tokens, group + 2, closing)
            if expansion is not None:
                expansions.update(range(group + 2, expansion + 1))
        index = group + 1
    return patterns, expansions


def _refuse_metavariable_modules(
    tokens: tuple[Token, ...],
    expansions: cabc.Iterable[int],
) -> None:
    """Fail closed on a ``mod $name;`` a macro expansion emits.

    The declaration's file is chosen by the macro's invocation, which the
    lexical scan never sees, so the audit cannot tell which file the crate
    compiles; reporting a clean scan would leave that file unread.  A literal
    ``mod name;`` in an expansion is left alone: the expansion spells the file
    itself, and module discovery already records it.  Only the expansion after
    ``=>`` is examined, so a matcher that spells ``mod $name;`` as the input it
    matches is not a declaration.
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


def _group_end(tokens: tuple[Token, ...], index: int, limit: int) -> int | None:
    """Return the close index of the group opening at ``index``, if it opens one."""
    if index >= limit:
        return None
    closer = _DELIMITER_PAIRS.get(tokens[index].text)
    if closer is None:
        return None
    return matching(tokens, index, tokens[index].text, closer)


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

    def __init__(
        self,
        tokens: tuple[Token, ...],
        patterns: cabc.Container[int] = frozenset(),
    ) -> None:
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
            if index not in patterns and _opens_inline_module(tokens, index):
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
