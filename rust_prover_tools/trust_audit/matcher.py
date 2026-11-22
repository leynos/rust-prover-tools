"""Decide which occurrences of the vocabulary are really requests for trust.

Finding a word is not the same as finding trust. ``assume`` is also an ordinary
identifier, ``should_panic`` is a standard test attribute, and ``external``
appears in prose and in paths. So a word counts only in a context that gives it
meaning:

1. an item reached through a path rooted at a prover's crate root --- ``kani``
   for Kani, ``vstd`` or ``verus_builtin`` for Verus --- including one the file
   renamed with ``use kani as k;`` or ``extern crate kani as k;``;
2. a word from a region-scoped vocabulary inside a ``verus!`` body, inside a
   ``verifier``, ``verus_verify`` or ``verus_spec`` attribute, or inside a
   ``cfg_attr`` that carries one of those;
3. any unqualified vocabulary word in a file that imports from one of those
   roots.

The vocabulary is supplied by the caller, so an audit can select one prover or
both.  The rules read the records they are given and know no prover by name.

Rule 3 reads wider than a glob or an alias on purpose: a *named* import such as
``use kani::assume;`` triggers it too, because a bare ``assume(..)`` after that
import is a real trust use no other rule reaches. The consequence is a
deliberate superset — a file that imports one Kani name and separately defines
its own ``external`` item reports the latter as well. Over-reporting leaves an
item for the reader to justify; under-reporting hides a request for trust
entirely, which is the direction that makes an audit worthless.

The scan refuses source it cannot reason about rather than reporting an empty
audit: ``include!``, ``paste!`` and ``concat_idents!`` generate code the lexer
never sees.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.trust_audit.context import Context
from rust_prover_tools.trust_audit.crate_bindings import alias_bindings
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.import_renames import (
    is_import_destination,
    is_import_rename,
)
from rust_prover_tools.trust_audit.macro_rules import MacroRules, macro_rules
from rust_prover_tools.trust_audit.model import ScanResult
from rust_prover_tools.trust_audit.modules import (
    inline_module_names,
    module_references,
)
from rust_prover_tools.trust_audit.repetitions import (
    MACRO_DELIMITERS,
    repetition_refusal,
)
from rust_prover_tools.trust_audit.structure import stringify_arguments
from rust_prover_tools.trust_audit.syntax import Kind, Token, tokenize
from rust_prover_tools.trust_audit.use_paths import statement_end, use_roots
from rust_prover_tools.trust_audit.use_tree import (
    begins_import,
    member_aliases,
)
from rust_prover_tools.trust_audit.vocabulary import (
    OPAQUE_MACROS,
    PROCEDURAL_OPAQUE_MACROS,
    PROVER_VOCABULARIES,
    Vocabulary,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("scan",)

# The qualifier, its ``::``, and the metavariable marker before a path segment.
_TRUST_PATH_SEGMENTS = 3
# The metavariable marker, ``as`` and the item before it.
_IMPORT_ALIAS_SEGMENTS = 3


def scan(
    text: str,
    *,
    procedural_macro: bool = False,
    vocabularies: tuple[Vocabulary, ...] = PROVER_VOCABULARIES,
) -> ScanResult:
    """Report every trusted item in one Rust source.

    Parameters
    ----------
    text : str
        Rust source text, which need not be a complete or valid crate.
    procedural_macro : bool
        Scan string literals as well. A proc-macro crate's literals can become
        code, so its vocabulary is not confined to code position.
    vocabularies : tuple[Vocabulary, ...]
        The prover vocabularies to apply.  Defaults to every known vocabulary;
        a caller narrows it to audit one prover.

    Returns
    -------
    ScanResult
        Items in source order, each reported once, plus every module file the
        source compiles for the caller to resolve.

    Raises
    ------
    TrustAuditError
        If a comment or literal never terminates, or the source uses a macro
        whose expansion the scan cannot see.

    Examples
    --------
    A call ``kani::assume(x);`` reports ``assume`` at line 1, column 7. The
    comment ``// kani::assume`` reports nothing, because a comment contributes
    no code token.
    """
    tokens = tokenize(text)
    words = frozenset(item for vocabulary in vocabularies for item in vocabulary.items)
    roots = frozenset(root for vocabulary in vocabularies for root in vocabulary.roots)
    try:
        qualifiers = set(roots)
        for root in roots:
            qualifiers.update(alias_bindings(tokens, root))
        _refuse_opaque_macros(
            tokens,
            procedural_macro=procedural_macro,
            words=words,
            qualifiers=frozenset(qualifiers),
        )
        modules = module_references(tokens)
        context = Context(
            tokens,
            procedural_macro=procedural_macro,
            vocabularies=vocabularies,
            module_names=frozenset(reference.name for reference in modules)
            | inline_module_names(tokens),
        )
    except RecursionError as error:
        # A grouped ``use`` nests, so a deeply nested one recurses through the
        # use-tree parser.  The audit's callers report a `TrustAuditError` as
        # `AUDIT_FAILED`; letting the interpreter's error escape would abort
        # them with a traceback instead.
        msg = "the file's use statements nest too deeply for the trust audit to read"
        raise TrustAuditError(msg) from error
    return ScanResult(context.run(), modules)


def _refuse_opaque_macros(
    tokens: tuple[Token, ...],
    *,
    procedural_macro: bool,
    words: frozenset[str],
    qualifiers: frozenset[str],
) -> None:
    """Fail closed on a macro whose expansion the scan cannot read.

    A rename is the same macro under another spelling: ``use std::include as
    load;`` makes ``load!`` expand as ``include!``, so the refusal follows the
    file's imports rather than only the literal name.

    ``include_str!`` and ``include_bytes!`` join the refused set only for a
    proc-macro crate.  Their expansions are a string literal and a byte array,
    which an ordinary crate's scan never reads; a proc-macro crate's literals
    are read as code, and such a crate can decode a spliced template and parse
    it into a token stream, so there the spliced text would carry code the
    lexer never saw.

    A matcher pattern is not an invocation: ``macro_rules! m { (include!($x)) =>
    … }`` describes input, and refusing it would abort an audit of a file that
    never calls ``include!``.
    """
    base = OPAQUE_MACROS
    if procedural_macro:
        base |= PROCEDURAL_OPAQUE_MACROS
    rules = macro_rules(tokens)
    names = _opaque_names(tokens, base, rules.patterns)
    # ``stringify!`` turns its argument into text, so an opaque macro inside
    # one does not run.  A proc-macro crate can parse that text, so there the
    # refusal stays.
    stringified = (
        frozenset()
        if procedural_macro
        else stringify_arguments(tokens, defined=rules.heads)
    )
    _refuse_metavariable_invocations(
        tokens,
        rules,
        _Vocabulary(words, qualifiers, stringified),
    )
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        if index in rules.patterns or index in stringified:
            continue
        if token.kind is not Kind.IDENT or token.text not in names:
            continue
        if following.text != "!":
            continue
        msg = (
            f"{token.text}! at line {token.line}, column {token.column} "
            "generates code the trust audit cannot read"
        )
        raise TrustAuditError(msg)


@dc.dataclass(frozen=True, slots=True)
class _Vocabulary:
    """The vocabulary and exclusions one refusal pass reads."""

    words: frozenset[str]
    qualifiers: frozenset[str]
    stringified: frozenset[int]


def _refuse_metavariable_invocations(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    scope: _Vocabulary,
) -> None:
    """Fail closed on an expansion whose metavariable hides a trust use.

    ``invoke!(include)`` expanding ``$m!("hidden.rs")`` becomes
    ``include!("hidden.rs")``, and ``call!(kani)`` expanding ``$p::assume(..)``
    becomes a Kani call.  The metavariable's value is an invocation argument the
    lexical scan does not evaluate, so no static answer names the macro or the
    path; the file is refused instead of reported clean.  ``$crate`` is the
    defining crate rather than an argument, so it is not refused.
    """
    for index in rules.expansions:
        if index in scope.stringified or not _is_expansion_metavariable(
            tokens,
            rules,
            index,
        ):
            continue
        token = tokens[index]
        if token.kind is not Kind.IDENT or token.text == "crate":
            continue
        refusal = _metavariable_refusal(
            tokens,
            index,
            scope.words,
            scope.qualifiers,
        )
        if refusal is not None:
            raise TrustAuditError(refusal)


def _is_expansion_metavariable(
    tokens: tuple[Token, ...],
    rules: MacroRules,
    index: int,
) -> bool:
    """Report whether ``index`` is a metavariable in executable expansion code.

    A matcher nested in an expansion is input the invocation fills in; it
    describes an invocation rather than making one, so its metavariables are
    skipped even though the matcher tokens also lie inside the outer expansion.
    """
    if index in rules.patterns or index < 1:
        return False
    return tokens[index - 1].text == "$"


def _metavariable_refusal(
    tokens: tuple[Token, ...],
    index: int,
    words: frozenset[str],
    qualifiers: frozenset[str],
) -> str | None:
    """Return the diagnostic for a hidden trust use, or ``None`` when clear."""
    token = tokens[index]
    following = tokens[index + 1].text if index + 1 < len(tokens) else ""
    second = tokens[index + 2].text if index + 2 < len(tokens) else ""
    if following == "!" and second in MACRO_DELIMITERS:
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} invokes a macro through the metavariable "
            f"'${token.text}', so the audit cannot tell which macro runs"
        )
    path_refusal = _path_refusal(token, following, second, words)
    if path_refusal is not None:
        return path_refusal
    if is_import_rename(tokens, index):
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} renames an import through the metavariable "
            f"'${token.text}', so the audit cannot tell which name it binds"
        )
    if is_import_destination(tokens, index):
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} binds an import's local name to the metavariable "
            f"'${token.text}', so the audit cannot tell which name it binds"
        )
    if _is_trust_qualified(tokens, index, qualifiers):
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a trust item through the metavariable "
            f"'${token.text}' as its final path segment, so the audit cannot "
            "tell which item is called"
        )
    return repetition_refusal(tokens, index, qualifiers)


def _path_refusal(
    token: Token,
    following: str,
    second: str,
    words: frozenset[str],
) -> str | None:
    """Return the diagnostic for a hidden ``::`` path, or ``None``."""
    if following != "::":
        return None
    if second == "$":
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a path whose segments are both "
            "metavariables, so the audit cannot tell which crate or item it names"
        )
    if second in words:
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a trust item through the metavariable "
            f"'${token.text}', so the audit cannot tell which crate it names"
        )
    if second in {"*", "{"}:
        return (
            f"the macro_rules! expansion at line {token.line}, column "
            f"{token.column} reaches a glob or grouped import through the "
            f"metavariable '${token.text}', so the audit cannot tell which "
            "names it binds"
        )
    return None


def _is_trust_qualified(
    tokens: tuple[Token, ...],
    index: int,
    qualifiers: frozenset[str],
) -> bool:
    """Report whether a metavariable ends a path rooted at a trust crate."""
    return (
        index >= _TRUST_PATH_SEGMENTS
        and tokens[index - 1].text == "$"
        and tokens[index - 2].text == "::"
        and tokens[index - 3].text in qualifiers
    )


def _opaque_names(
    tokens: tuple[Token, ...],
    base: frozenset[str],
    patterns: cabc.Container[int],
) -> frozenset[str]:
    """Return the opaque macro names, plus every local name bound to one.

    A re-export can pass a name through more than one module —
    ``mod m { pub use std::include as load; } use m::load as hidden;`` — so the
    search reads every ``as`` rename in the file and repeats until a pass adds
    nothing.  Following only the roots of the statements would record
    ``include → load`` and ``load → hidden`` but never ``include → hidden``,
    and ``hidden!`` would be read as an ordinary macro.  A ``use`` written in a
    matcher is input, not an import, so those tokens are skipped.
    """
    names = set(base)
    renames = _all_renames(tokens, patterns)
    while True:
        discovered = {alias for item, alias in renames if item in names}
        if discovered <= names:
            return frozenset(names)
        names |= discovered


def _all_renames(
    tokens: tuple[Token, ...],
    patterns: cabc.Container[int],
) -> frozenset[tuple[str, str]]:
    """Return every ``(item, alias)`` a ``use`` statement in the file renames.

    A rename of a macro's item (``use std::include as load;``) and a rename of
    the name it bound (``use load as hidden;``) are both followed: the second
    spelling is the same opaque macro, and stopping at the first would let
    ``hidden!`` expand code the scan never reads.  Tokens inside a
    ``macro_rules!`` matcher describe input and import nothing.
    """
    renames: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if index in patterns:
            continue
        if token.kind is not Kind.IDENT or not begins_import(tokens, index):
            continue
        end = statement_end(tokens, index + 1)
        renames.update(member_aliases(tokens, index + 1, end))
        renames.update(
            (root, alias)
            for root, alias in use_roots(tokens, index + 1, end)
            if alias is not None
        )
    return frozenset(renames)
