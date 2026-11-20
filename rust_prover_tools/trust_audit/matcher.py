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
import typing as typ

from rust_prover_tools.trust_audit.model import ScanResult, TrustItem
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.structure import attribute_spans, matching
from rust_prover_tools.trust_audit.syntax import (
    Kind,
    Token,
    TrustAuditError,
    literal_body,
    tokenize,
)
from rust_prover_tools.trust_audit.use_tree import (
    alias_bindings,
    statement_end,
    use_roots,
)
from rust_prover_tools.trust_audit.vocabulary import (
    KANI_ROOT,
    KANI_VOCABULARY,
    OPAQUE_MACROS,
    TRUST_ROOTS,
    VERUS_ATTRIBUTE_HEADS,
    VERUS_VOCABULARY,
    VOCABULARY,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("scan",)

# ``verus``, ``!``, ``{``: the head of a ``verus! { … }`` body.
_VERUS_MACRO_HEAD = ("verus", "!", "{")
# The ``::`` and the qualifier in front of a path's last segment.
_QUALIFIED_SEGMENTS = 2
# ``extern crate``: the two words that open a crate declaration.
_EXTERN_CRATE = ("extern", "crate")
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
    _refuse_opaque_macros(tokens)
    context = _Context(tokens, procedural_macro=procedural_macro)
    return ScanResult(context.run(), module_references(tokens))


def _refuse_opaque_macros(tokens: tuple[Token, ...]) -> None:
    """Fail closed on a macro whose expansion the scan cannot read."""
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        if token.kind is not Kind.IDENT or token.text not in OPAQUE_MACROS:
            continue
        if following.text != "!":
            continue
        msg = (
            f"{token.text}! at line {token.line}, column {token.column} "
            "generates code the trust audit cannot read"
        )
        raise TrustAuditError(msg)


class _Context:
    """Hold one file's lexical facts and accumulate the items it reports.

    The facts are file-scoped rather than local: rule 3 is decided by an import
    that may sit on the other side of the file from the use it makes ambiguous.
    """

    def __init__(self, tokens: tuple[Token, ...], *, procedural_macro: bool) -> None:
        """Record the tokens and the file-scoped facts the rules depend on."""
        self.tokens = tokens
        self.imported_roots = _imported_roots(tokens)
        # A trust root may be in scope under another name. ``use kani as k;``
        # makes ``k::assume`` the same call as ``kani::assume``, and a scan that
        # matched only the literal root would report nothing for the file.
        self.kani_names = alias_bindings(tokens, KANI_ROOT)
        self.verus_regions = _verus_regions(tokens)
        self.items: list[TrustItem] = []
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

        Each rule uses the vocabulary of the system it names: a ``kani::`` path
        is Kani's, and a Verus body or attribute is Verus'. A Kani-only word
        inside a ``verus!`` body is not Verus trust, so it is not reported
        there; rule 3 covers the file that imports from a trust root, and
        reporting it here would attach a misleading claim to the location.
        """
        if self._is_kani_path(index):
            self._report(token.text, token)
        elif self._is_verus_context(index):
            if token.text in VERUS_VOCABULARY:
                self._report(token.text, token)
        else:
            self._report_if_imported(index, token)

    def _report_if_imported(self, index: int, token: Token) -> None:
        """Apply rule 3: a bare word in a file that imports from a trust root."""
        if not self.imported_roots:
            return
        if not _is_bare_use(self.tokens, index):
            return
        if token.text in VOCABULARY:
            self._report(token.text, token)

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
        """Append the item unless the same word at the same place is recorded."""
        if item not in self.items:
            self.items.append(item)

    def _scan_literal(self, token: Token) -> None:
        r"""Report vocabulary inside a proc-macro crate's string literal.

        The body keeps its backslashes, so a ``\\n`` there is two characters
        rather than a newline. Translating it would make the offset arithmetic
        disagree with what the author sees in an editor, so the body is scanned
        as written and positions count characters.
        """
        offset, body = literal_body(token.text)
        for match in _QUALIFIED_KANI.finditer(body):
            if match[1] in KANI_VOCABULARY:
                self._record(_literal_item(match[1], token, offset + match.start(1)))
        for match in _WORD.finditer(body):
            if match[0] in VOCABULARY:
                self._record(_literal_item(match[0], token, offset + match.start()))


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


def _imported_roots(tokens: tuple[Token, ...]) -> frozenset[str]:
    """Return the trust roots this file imports, by ``use`` or ``extern crate``.

    Every root of every statement counts, not just its leading word.
    ``use {std::mem, kani};`` imports ``kani`` as surely as the ungrouped form
    does, and reading only the first identifier would quietly switch rule 3 off
    for that file.

    ``extern crate kani;`` is the same import spelled without a ``use``, and it
    likewise makes a bare ``assume`` resolve to Kani, so it counts too.
    """
    roots = set()
    for index, token in enumerate(tokens):
        if token.kind is not Kind.IDENT:
            continue
        declared = _extern_crate_root(tokens, token, index)
        if declared is not None:
            roots.add(declared)
        if token.text != "use":
            continue
        end = statement_end(tokens, index + 1)
        roots.update(root for root, _ in use_roots(tokens, index + 1, end))
    return frozenset(roots & TRUST_ROOTS)


def _extern_crate_root(
    tokens: tuple[Token, ...],
    token: Token,
    index: int,
) -> str | None:
    """Return the crate an ``extern crate`` declaration at ``index`` names."""
    if _texts(tokens, index, len(_EXTERN_CRATE)) != _EXTERN_CRATE:
        return None
    name_index = index + len(_EXTERN_CRATE)
    if name_index >= len(tokens):
        return None
    name = tokens[name_index]
    return name.text if name.kind is Kind.IDENT else None


def _texts(tokens: tuple[Token, ...], start: int, count: int) -> tuple[str, ...]:
    """Return up to ``count`` token texts from ``start``, clipped at the end."""
    return tuple(token.text for token in tokens[start : start + count])


def _verus_regions(tokens: tuple[Token, ...]) -> tuple[tuple[int, int], ...]:
    """Return the token ranges whose identifiers carry Verus' own vocabulary.

    Two forms create a region: a ``verus!`` body, and a Verus attribute anywhere
    in an attribute list, including one nested in a ``cfg_attr``.
    """
    regions = list(_verus_bodies(tokens))
    for open_index, close_index in attribute_spans(tokens):
        regions.extend(_verus_attribute_regions(tokens, open_index, close_index))
    return tuple(regions)


def _verus_bodies(tokens: tuple[Token, ...]) -> cabc.Iterator[tuple[int, int]]:
    """Locate each ``verus! { … }`` body, tolerating nesting."""
    for index in range(len(tokens)):
        if _texts(tokens, index, len(_VERUS_MACRO_HEAD)) != _VERUS_MACRO_HEAD:
            continue
        opening = index + len(_VERUS_MACRO_HEAD) - 1
        yield opening + 1, matching(tokens, opening, "{", "}")


def _verus_attribute_regions(
    tokens: tuple[Token, ...],
    open_index: int,
    close_index: int,
) -> tuple[tuple[int, int], ...]:
    """Return the range of an attribute whose identifiers are Verus vocabulary."""
    head_index = open_index + 1
    if head_index >= close_index:
        return ()
    if tokens[head_index].text in VERUS_ATTRIBUTE_HEADS:
        return ((head_index, close_index - 1),)
    if tokens[head_index].text != "cfg_attr":
        return ()
    if _carries_verus(tokens, head_index + 1, close_index):
        return ((head_index, close_index - 1),)
    return ()


def _carries_verus(tokens: tuple[Token, ...], start: int, end: int) -> bool:
    """Report whether a ``cfg_attr`` carries a Verus attribute after its predicate."""
    return any(
        token.kind is Kind.IDENT and token.text in VERUS_ATTRIBUTE_HEADS
        for token in tokens[start:end]
    )
