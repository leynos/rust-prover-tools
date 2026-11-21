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

from rust_prover_tools.trust_audit.context import Context
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.model import ScanResult
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.syntax import Kind, Token, tokenize
from rust_prover_tools.trust_audit.use_bindings import member_bindings
from rust_prover_tools.trust_audit.use_tree import file_roots
from rust_prover_tools.trust_audit.vocabulary import (
    OPAQUE_MACROS,
    PROCEDURAL_OPAQUE_MACROS,
    PROVER_VOCABULARIES,
    Vocabulary,
)

__all__ = ("scan",)


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
    try:
        _refuse_opaque_macros(tokens, procedural_macro=procedural_macro)
        context = Context(
            tokens,
            procedural_macro=procedural_macro,
            vocabularies=vocabularies,
        )
    except RecursionError as error:
        # A grouped ``use`` nests, so a deeply nested one recurses through the
        # use-tree parser.  The audit's callers report a `TrustAuditError` as
        # `AUDIT_FAILED`; letting the interpreter's error escape would abort
        # them with a traceback instead.
        msg = "the file's use statements nest too deeply for the trust audit to read"
        raise TrustAuditError(msg) from error
    return ScanResult(context.run(), module_references(tokens))


def _refuse_opaque_macros(
    tokens: tuple[Token, ...],
    *,
    procedural_macro: bool,
) -> None:
    """Fail closed on a macro whose expansion the scan cannot read.

    A rename is the same macro under another spelling: ``use std::include as
    load;`` makes ``load!`` expand as ``include!``, so the refusal follows the
    file's imports rather than only the literal name.

    ``include_str!`` joins the refused set only for a proc-macro crate.  Its
    expansion is a string literal, which an ordinary crate's scan never reads;
    a proc-macro crate's literals are read as code, so there the spliced file
    would carry code the lexer never saw.
    """
    base = OPAQUE_MACROS
    if procedural_macro:
        base |= PROCEDURAL_OPAQUE_MACROS
    names = _opaque_names(tokens, base)
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        if token.kind is not Kind.IDENT or token.text not in names:
            continue
        if following.text != "!":
            continue
        msg = (
            f"{token.text}! at line {token.line}, column {token.column} "
            "generates code the trust audit cannot read"
        )
        raise TrustAuditError(msg)


def _opaque_names(
    tokens: tuple[Token, ...],
    base: frozenset[str],
) -> frozenset[str]:
    """Return the opaque macro names, plus every local name bound to one."""
    names = set(base)
    names.update(
        alias
        for item, alias in member_bindings(tokens, file_roots(tokens))
        if item in base
    )
    return frozenset(names)
