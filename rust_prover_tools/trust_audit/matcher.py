"""Decide which occurrences of the vocabulary are really requests for trust.

Finding a word is not the same as finding trust. ``assume`` is also an ordinary
identifier, ``should_panic`` is a standard test attribute, and ``external``
appears in prose and in paths. So a word counts only in a context that gives it
meaning:

1. a Kani item reached through a path rooted at ``kani``, including one the file
   renamed with ``use kani as k;`` or ``extern crate kani as k;``;
2. a Verus word inside a ``verus!`` body, inside a ``verifier``,
   ``verus_verify`` or ``verus_spec`` attribute, or inside a ``cfg_attr`` that
   carries one of those;
3. any unqualified vocabulary word in a file that imports from one of those
   roots.

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

import re

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.literals import decoded_literal_body_positions
from rust_prover_tools.trust_audit.model import ScanResult, TrustItem
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.regions import verus_regions
from rust_prover_tools.trust_audit.syntax import Kind, Token, tokenize
from rust_prover_tools.trust_audit.use_bindings import alias_bindings, member_bindings
from rust_prover_tools.trust_audit.use_tree import file_roots, imported_roots
from rust_prover_tools.trust_audit.vocabulary import (
    KANI_ROOT,
    KANI_VOCABULARY,
    OPAQUE_MACROS,
    PROCEDURAL_OPAQUE_MACROS,
    TRUST_ROOTS,
    VERUS_VOCABULARY,
    VOCABULARY,
)

__all__ = ("scan",)

# The ``::`` and the qualifier in front of a path's last segment.
_QUALIFIED_SEGMENTS = 2
# A qualified Kani path inside a proc-macro string literal.
_QUALIFIED_KANI = re.compile(r"\bkani\s*::\s*([A-Za-z_][A-Za-z0-9_]*)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def scan(text: str, *, procedural_macro: bool = False) -> ScanResult:
    """Report every trusted item in one Rust source.

    Parameters
    ----------
    text : str
        Rust source text, which need not be a complete or valid crate.
    procedural_macro : bool
        Scan string literals as well. A proc-macro crate's literals can become
        code, so its vocabulary is not confined to code position.

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
        context = _Context(tokens, procedural_macro=procedural_macro)
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


class _Context:
    """Hold one file's lexical facts and accumulate the items it reports.

    The facts are file-scoped rather than local: rule 3 is decided by an import
    that may sit on the other side of the file from the use it makes ambiguous.
    """

    def __init__(self, tokens: tuple[Token, ...], *, procedural_macro: bool) -> None:
        """Record the tokens and the file-scoped facts the rules depend on."""
        self.tokens = tokens
        self.imported_roots = imported_roots(tokens, TRUST_ROOTS)
        # A trust root may be in scope under another name. ``use kani as k;``
        # makes ``k::assume`` the same call as ``kani::assume``, and a scan that
        # matched only the literal root would report nothing for the file.
        self.kani_names = alias_bindings(tokens, KANI_ROOT)
        # A renamed member is the same request for trust under another name:
        # after ``use kani::{assume as a};`` a bare ``a(..)`` is Kani's call.
        # The crate may itself have been renamed first — ``use kani as k;``
        # then ``use k::{assume as a};`` — so the Kani aliases count as roots.
        self.member_names = _member_names(tokens, TRUST_ROOTS | self.kani_names)
        self.verus_regions = verus_regions(tokens)
        self.items: list[TrustItem] = []
        self._reported: set[TrustItem] = set()
        self._procedural_macro = procedural_macro

    def run(self) -> tuple[TrustItem, ...]:
        """Apply the rules to every token, in source order."""
        for index, token in enumerate(self.tokens):
            if token.kind is Kind.LITERAL:
                if self._procedural_macro:
                    self._scan_literal(token)
            elif token.kind is Kind.IDENT:
                self._classify(index, token)
        return tuple(self.items)

    def _classify(self, index: int, token: Token) -> None:
        """Report the token when any rule makes it trust.

        The rules are cumulative rather than exclusive. Each uses the
        vocabulary of the system it names: a ``kani::`` path is Kani's, and a
        Verus body or attribute is Verus'. A Kani-only word inside a
        ``verus!`` body is not Verus vocabulary, so rule 2 leaves it alone, but
        rule 3 still reports it when the file imports from a trust root. That
        is the same fail-closed superset rule 3 applies everywhere else:
        under-reporting hides a request for trust, while over-reporting leaves
        an item for the reader to justify.
        """
        if self._is_kani_path(index):
            self._report(token.text, token)
            return
        if token.text in VERUS_VOCABULARY and self._is_verus_context(index):
            self._report(token.text, token)
        self._report_if_imported(index, token)

    def _report_if_imported(self, index: int, token: Token) -> None:
        """Apply rule 3: a bare word in a file that imports from a trust root.

        A renamed import is reported under the item it names, at the position
        where it is used: after ``use kani::{assume as a};`` the call ``a(x)``
        is Kani's ``assume``, and reporting the local spelling would leave the
        audit unable to say which prover surface it reached.  ``self::a(..)``
        names the item the same import bound, so it counts too; another
        module's qualifier does not, because the binding there is not shown.
        """
        if not self.imported_roots:
            return
        if _is_bare_use(self.tokens, index) and token.text in VOCABULARY:
            self._report(token.text, token)
            return
        items = self.member_names.get(token.text)
        if items is None or not _is_member_use(self.tokens, index):
            return
        if _is_bare_use(self.tokens, index) or _is_self_qualified(self.tokens, index):
            for item in sorted(items):
                self._report(item, token)

    def _is_kani_path(self, index: int) -> bool:
        """Report whether this token ends a path rooted at Kani.

        A ``use kani as k;`` in the file makes ``k::`` the same qualified path,
        so the qualifier is matched against the literal root and against every
        local name the file binds it to.
        """
        if self.tokens[index].text not in KANI_VOCABULARY:
            return False
        if index < _QUALIFIED_SEGMENTS:
            return False
        separator = self.tokens[index - 1]
        qualifier = self.tokens[index - _QUALIFIED_SEGMENTS]
        return separator.text == "::" and qualifier.text in self.kani_names

    def _is_verus_context(self, index: int) -> bool:
        """Report whether this index sits in a Verus body or attribute."""
        return any(start <= index <= end for start, end in self.verus_regions)

    def _report(self, item: str, token: Token) -> None:
        """Record one item, skipping a position an earlier rule claimed.

        One token can satisfy several rules — the ``stub`` in
        ``kani::stub(..)`` inside a ``cfg_attr`` satisfies both 1 and 2 — and
        the audit must report it once.
        """
        self._record(TrustItem(item, token.line, token.column))

    def _record(self, item: TrustItem) -> None:
        """Append the item unless the same word at the same place is recorded.

        The set answers "already reported" in constant time; the list keeps the
        source order the audit promises, which is what a report is read in.
        """
        if item in self._reported:
            return
        self._reported.add(item)
        self.items.append(item)

    def _scan_literal(self, token: Token) -> None:
        r"""Report vocabulary inside a proc-macro crate's string literal.

        A cooked literal's escapes are decoded, because what the macro receives
        is the value rather than the spelling: ``"kani::\x61ssume(..)"`` is a
        generated call to Kani's ``assume``. Each match is reported at the
        source offset of the character that produced it, so the report names
        the escape the author wrote rather than a position inside the value. A
        raw literal has no escapes, so its body is read as written.

        The qualified and bare searches overlap, and one may reach a later
        position before the other reaches an earlier one, so both are collected
        and then reported in source order.
        """
        _, body, positions = decoded_literal_body_positions(token.text)
        found = [
            (match.start(1), match[1])
            for match in _QUALIFIED_KANI.finditer(body)
            if match[1] in KANI_VOCABULARY
        ]
        found.extend(
            (match.start(), match[0])
            for match in _WORD.finditer(body)
            if match[0] in VOCABULARY
        )
        for start, item in sorted(found):
            self._record(_literal_item(item, token, positions[start]))


def _literal_item(item: str, token: Token, offset: int) -> TrustItem:
    """Locate a literal match precisely enough that two of them stay distinct.

    Positions outside a literal are token positions, which the lexer already
    counts. Inside one, the token is opaque, so the offset is measured from the
    token's start and advanced over any newline the literal contains.
    """
    prefix = token.text[:offset]
    if "\n" not in prefix:
        return TrustItem(item, token.line, token.column + offset)
    line = token.line + prefix.count("\n")
    return TrustItem(item, line, len(prefix) - prefix.rindex("\n"))


def _is_bare_use(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether rule 3 makes this token a bare use.

    A word after ``::`` belongs to a path and a word after ``.`` to a field or
    method; neither is the unqualified use rule 3 is about. Rule 1 has already
    claimed the ``kani::`` case, so only the remaining positions matter.
    """
    if not index:
        return True
    return tokens[index - 1].text not in {"::", "."}


def _member_names(
    tokens: tuple[Token, ...],
    roots: frozenset[str],
) -> dict[str, frozenset[str]]:
    """Group every vocabulary member by the local name that denotes it.

    Two scopes may bind one local name to different items — ``use kani::assume
    as m;`` in one module and ``use kani::any_where as m;`` in another — and
    the scan is file-scoped, so it cannot tell which scope a later ``m(..)``
    sits in.  Keeping every item the name may denote, and reporting all of
    them, leaves the item for the reader to justify rather than dropping one.
    """
    names: dict[str, set[str]] = {}
    for member, alias in member_bindings(tokens, roots):
        if member in VOCABULARY:
            names.setdefault(alias, set()).add(member)
    return {alias: frozenset(items) for alias, items in names.items()}


def _is_self_qualified(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether this token is named through ``self::``.

    A named import binds its item in the module it appears in, so
    ``self::a(..)`` after ``use kani::assume as a;`` is the same call as the
    bare ``a(..)``.  The qualifier is ``self`` rather than another module, so
    it names the binding rather than turning the name into a path through an
    item.
    """
    return (
        index >= _QUALIFIED_SEGMENTS
        and tokens[index - 1].text == "::"
        and tokens[index - _QUALIFIED_SEGMENTS].text == "self"
    )


def _is_member_use(tokens: tuple[Token, ...], index: int) -> bool:
    """Report whether a renamed import is used as the item it renames.

    Only the bare call counts. The name at its own definition follows ``as``,
    and a name in front of ``::`` qualifies a path rather than calling the
    item, so neither is a request for trust under another spelling.
    """
    if index and tokens[index - 1].text == "as":
        return False
    return index + 1 >= len(tokens) or tokens[index + 1].text != "::"
