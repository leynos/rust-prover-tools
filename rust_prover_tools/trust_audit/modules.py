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

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.inline_modules import (
    DECLARATION_TOKENS,
    InlineModules,
    inline_module_names,
    names_module,
    path_attribute,
)
from rust_prover_tools.trust_audit.macro_rules import MacroRules, macro_rules
from rust_prover_tools.trust_audit.model import InlineModule, ModuleReference
from rust_prover_tools.trust_audit.module_arguments import refuse_forwarded_modules
from rust_prover_tools.trust_audit.structure import (
    attribute_spans,
    stringify_arguments,
)
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import begins_import, member_aliases

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("inline_module_names", "module_references")

# What follows a module name: a ``;`` for an external module.
_EXTERNAL = ";"
# The delimiter pairs a macro invocation's body may open.
_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}
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
    stringified = stringify_arguments(tokens, defined=rules.heads)
    _refuse_metavariable_modules(tokens, rules.expansions, stringified)
    aliases = _macro_aliases(tokens, rules.heads)
    refuse_forwarded_modules(tokens, rules, aliases, stringified)
    inline = InlineModules(tokens, rules.patterns)
    contexts = _invocation_contexts(
        tokens,
        rules,
        inline,
        _WalkScope(aliases, stringified),
    )
    for index, token in enumerate(tokens):
        head = tokens[index + 1 : index + DECLARATION_TOKENS]
        if index in rules.patterns or index in stringified:
            continue
        if not names_module(token, head) or head[1].text != _EXTERNAL:
            continue
        name = head[0]
        path = path_attribute(tokens, spans, index)
        if index in rules.expansions:
            references.extend(
                _expanded_references(
                    contexts,
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


@dc.dataclass(frozen=True, slots=True)
class _WalkScope:
    """The alias map and stringified spans one expansion walk reads."""

    aliases: cabc.Mapping[str, str]
    stringified: frozenset[int]


def _invocation_contexts(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    inline: InlineModules,
    scope: _WalkScope,
) -> dict[int, tuple[tuple[InlineModule, ...], ...]]:
    """Return every context an expansion token compiles under.

    A macro invoked inside another macro's expansion expands after the outer
    call, so the inner declaration resolves from the outer invocation's
    directory plus whatever inline modules the inner expansion declares.  The
    traversal follows those calls; a recursive definition is refused, because
    the chain would never end and the module tree could not be reported.
    """
    walker = _ContextWalker(tokens, rules, inline, scope)
    for name in rules.heads:
        for site in _invocation_sites(rules, scope.aliases, name):
            if site not in rules.definitions and site not in scope.stringified:
                walker.enter(name, inline.enclosing(site))
    return walker.frozen()


def _macro_aliases(
    tokens: tuple[Token, ...],
    heads: cabc.Container[str],
) -> dict[str, str]:
    """Return every local name the file binds to a macro it defines.

    Rust expands ``use crate::make as build; build!();`` as the definition of
    ``make``, so the invocation site is the same site under another spelling.
    An alias can itself be renamed — ``use build as rebuild;`` — so discovery
    repeats until a pass adds no name, and each alias records the defined macro
    rather than the intermediate spelling.
    """
    renames = _all_macro_renames(tokens)
    aliases: dict[str, str] = {}
    while True:
        fresh = _fresh_macro_aliases(renames, heads, aliases)
        for alias, target in fresh.items():
            if alias in heads and alias != target:
                msg = (
                    f"the local macro name {alias!r} is both a definition and "
                    f"an alias for {target!r}, so the audit cannot tell which "
                    "one a textual invocation expands"
                )
                raise TrustAuditError(msg)
        if not fresh:
            return aliases
        aliases.update(fresh)


def _fresh_macro_aliases(
    renames: cabc.Iterable[tuple[str, str]],
    heads: cabc.Container[str],
    aliases: cabc.Mapping[str, str],
) -> dict[str, str]:
    """Return this pass's newly resolved aliases, refusing a conflict.

    Two modules may bind one local name to different macros, and the per-file
    scan cannot tell which definition an invocation sees, so that ambiguity is
    refused rather than resolved arbitrarily.
    """
    fresh: dict[str, str] = {}
    for item, alias in sorted(renames):
        if item in heads:
            target = item
        elif item in aliases:
            target = aliases[item]
        else:
            continue
        known = aliases.get(alias, fresh.get(alias))
        if known is not None and known != target:
            msg = (
                f"the local macro name {alias!r} is bound to both {known!r} "
                f"and {target!r}, so the audit cannot tell which definition "
                "an invocation expands"
            )
            raise TrustAuditError(msg)
        if alias not in aliases:
            fresh[alias] = target
    return fresh


def _all_macro_renames(tokens: tuple[Token, ...]) -> frozenset[tuple[str, str]]:
    """Return every ``(item, alias)`` a ``use`` statement in the file renames.

    A macro is renamed either as a member (``use crate::make as build;``) or as
    the statement's root (``use build as rebuild;``), so both readings are
    collected.
    """
    renames: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        renames.update(
            (root, alias)
            for root, alias in use_roots(tokens, index + 1, end)
            if alias is not None
        )
        renames.update(member_aliases(tokens, index + 1, end))
    return frozenset(renames)


def _invocation_sites(
    rules: MacroRules,
    aliases: cabc.Mapping[str, str],
    name: str,
) -> tuple[int, ...]:
    """Return every site that invokes ``name``, directly or through an alias.

    One statement may rename a macro to its own name, and several aliases may
    name the same macro, so the sites are de-duplicated while keeping the
    source order the traversal records them in.
    """
    sites = dict.fromkeys(rules.invocations.get(name, ()))
    for alias, target in aliases.items():
        if target == name:
            sites.update(dict.fromkeys(rules.invocations.get(alias, ())))
    return tuple(sites)


@dc.dataclass(slots=True)
class _ContextWalker:
    """Walk macro invocations, recording each expansion's enclosing chains."""

    tokens: tuple[Token, ...]
    rules: MacroRules
    inline: InlineModules
    scope: _WalkScope
    contexts: dict[int, list[tuple[InlineModule, ...]]] = dc.field(
        default_factory=dict,
    )
    visiting: set[str] = dc.field(default_factory=set)

    def enter(self, name: str, outer: tuple[InlineModule, ...]) -> None:
        """Record the expansion of ``name`` under one invocation context."""
        head = self.rules.heads.get(name)
        if head is None:
            return
        if name in self.visiting:
            msg = (
                f"the macro {name!r} expands recursively, so the audit cannot "
                "tell where its module declarations resolve"
            )
            raise TrustAuditError(msg)
        self.visiting.add(name)
        definition_chain = self.inline.enclosing(head)
        for token_index in self.rules.expansions_by_name.get(name, ()):
            self._expand(token_index, outer, definition_chain)
        self.visiting.remove(name)

    def _expand(
        self,
        token_index: int,
        outer: tuple[InlineModule, ...],
        definition_chain: tuple[InlineModule, ...],
    ) -> None:
        """Record one expansion token and follow a macro it invokes."""
        local = self.inline.enclosing(token_index)[len(definition_chain) :]
        chain = (*outer, *local)
        self.contexts.setdefault(token_index, []).append(chain)
        invoked = _invoked_macro(
            self.tokens,
            self.rules,
            self.scope.aliases,
            token_index,
        )
        if invoked is not None and token_index not in self.scope.stringified:
            self.enter(invoked, chain)

    def frozen(self) -> dict[int, tuple[tuple[InlineModule, ...], ...]]:
        """Freeze the recorded chains into the mapping the caller reads."""
        return {index: tuple(chains) for index, chains in self.contexts.items()}


def _invoked_macro(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    aliases: cabc.Mapping[str, str],
    index: int,
) -> str | None:
    """Return the macro a token inside an expansion invokes, if it invokes one.

    An invocation through a ``use`` alias is the definition's macro, so the
    local spelling is resolved before the name is looked up.
    """
    if index not in rules.expansions or index + 2 >= len(tokens):
        return None
    token = tokens[index]
    if token.kind is not Kind.IDENT or tokens[index + 1].text != "!":
        return None
    if tokens[index + 2].text not in _DELIMITER_PAIRS:
        return None
    name = token.text if token.text in rules.heads else aliases.get(token.text)
    return name if name in rules.heads else None


def _expanded_references(
    contexts: cabc.Mapping[int, tuple[tuple[InlineModule, ...], ...]],
    token_index: int,
    reference: ModuleReference,
) -> tuple[ModuleReference, ...]:
    """Attribute one expansion's declaration to each context it compiles in.

    A macro no invocation in this file names has no context, so it compiles
    nothing here and the reference is dropped.
    """
    return tuple(
        dc.replace(reference, enclosing=chain)
        for chain in contexts.get(token_index, ())
    )


def _refuse_metavariable_modules(
    tokens: tuple[Token, ...],
    expansions: cabc.Iterable[int],
    stringified: frozenset[int],
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
        if index in stringified:
            continue
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
