"""Apply the trust rules to one file's lexed tokens.

`rust_prover_tools.trust_audit.matcher` decides *what* a scan is for and
refuses macros whose expansion it cannot see; this module holds the per-file
state those rules read.  A note on the constants:

- ``_QUALIFIED_SEGMENTS`` is the ``::`` and the qualifier in front of a path's
  last segment.
- ``_WORD`` matches an identifier inside a proc-macro string literal.
"""

from __future__ import annotations

import bisect
import re
import typing as typ

from rust_prover_tools.trust_audit.inline_modules import declared_module_aliases
from rust_prover_tools.trust_audit.literals import (
    decoded_literal_body_positions,
    qualified_pattern,
)
from rust_prover_tools.trust_audit.model import TrustItem
from rust_prover_tools.trust_audit.positions import (
    imports_root_glob,
    is_bare_use,
    is_member_position,
    is_member_use,
    member_names,
)
from rust_prover_tools.trust_audit.regions import verus_regions
from rust_prover_tools.trust_audit.syntax import Kind, Token
from rust_prover_tools.trust_audit.use_bindings import alias_bindings
from rust_prover_tools.trust_audit.use_tree import imported_roots

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.trust_audit.vocabulary import Vocabulary

__all__ = ("Context",)

# The ``::`` and the qualifier in front of a path's last segment.
_QUALIFIED_SEGMENTS = 2
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Context:
    """Hold one file's lexical facts and accumulate the items it reports.

    The facts are file-scoped rather than local: rule 3 is decided by an import
    that may sit on the other side of the file from the use it makes ambiguous.
    """

    def __init__(
        self,
        tokens: tuple[Token, ...],
        *,
        procedural_macro: bool,
        vocabularies: tuple[Vocabulary, ...],
        module_names: frozenset[str] = frozenset(),
    ) -> None:
        """Record the tokens and the file-scoped facts the rules depend on.

        ``module_names`` holds the modules this file declares.  A qualified use
        such as ``m::a`` names a local binding when ``m`` is one of them; a
        module declared elsewhere is not shown, so its names are not read.
        """
        self.tokens = tokens
        self.vocabularies = vocabularies
        # ``use m as n;`` makes ``n::a`` the same call as ``m::a`` when the
        # file declares ``m``, so the alias is a module name for rule 3.
        self.module_names = module_names | declared_module_aliases(
            tokens,
            module_names,
        )
        self.words = frozenset(
            item for vocabulary in vocabularies for item in vocabulary.items
        )
        self.region_words = frozenset(
            item
            for vocabulary in vocabularies
            if vocabulary.regions
            for item in vocabulary.items
        )
        self.roots = frozenset(
            root for vocabulary in vocabularies for root in vocabulary.roots
        )
        # A trust root may be in scope under another name. ``use kani as k;``
        # makes ``k::assume`` the same call as ``kani::assume``, and a scan that
        # matched only the literal root would report nothing for the file.  Each
        # root keeps its own aliases, because an alias names that root only.
        self.root_names = {
            root: alias_bindings(tokens, root) for root in sorted(self.roots)
        }
        all_root_names = frozenset(
            name for names in self.root_names.values() for name in names
        )
        # The aliases count as import roots here: a file whose only link to a
        # trust root is ``use crate::k;`` must still reach the member rules,
        # and that link is visible only through the alias set.
        self.imported_roots = imported_roots(
            tokens,
            self.roots | all_root_names,
        )
        # A renamed member is the same request for trust under another name:
        # after ``use kani::{assume as a};`` a bare ``a(..)`` is Kani's call.
        # The crate may itself have been renamed first — ``use kani as k;``
        # then ``use k::{assume as a};`` — so the aliases count as roots.
        self.member_names = member_names(
            tokens,
            self.roots | all_root_names,
            self.words,
        )
        # ``use kani::*;`` binds every exported name, including one a declared
        # module re-exports as ``m::assume``.  The scan cannot enumerate what
        # the glob binds, so a qualified vocabulary word is reported wherever
        # it appears; the same fail-closed direction rule 3 takes.
        self.root_glob = imports_root_glob(tokens, self.roots | all_root_names)
        self.verus_regions = verus_regions(tokens)
        self.qualified = qualified_pattern(self.roots)
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
        if self._is_rooted_path(index):
            self._report(token.text, token)
            return
        if self.root_glob and self._is_glob_qualified(index, token):
            self._report(token.text, token)
            return
        if token.text in self.region_words and self._is_verus_context(index):
            self._report(token.text, token)
        self._report_if_imported(index, token)

    def _names_member(self, index: int, token: Token) -> bool:
        """Report whether this token reaches an item a local import bound."""
        items = self.member_names.get(token.text)
        if items is None or not is_member_use(self.tokens, index):
            return False
        return is_member_position(self.tokens, index, self.module_names)

    def _report_if_imported(self, index: int, token: Token) -> None:
        """Apply rule 3: a bare word in a file that imports from a trust root.

        A renamed import is reported under the item it names, at the position
        where it is used: after ``use kani::{assume as a};`` the call ``a(x)``
        is Kani's ``assume``, and reporting the local spelling would leave the
        audit unable to say which prover surface it reached.  The qualifiers
        that name the binding itself — ``self::``, ``crate::`` and ``super::``
        — count too, and so does a module this file declares, because its
        import can be read here: ``mod m { pub use kani::assume as a; }`` makes
        ``m::a`` the same call.  A module declared elsewhere is not shown, so
        its names are left alone.
        """
        if not self.imported_roots:
            return
        # A local spelling may be both a vocabulary word and a rename.  The
        # binding says which item the call reaches, so it is read first:
        # ``use kani::assume as any_where;`` must report ``assume``, not the
        # word ``any_where`` the author borrowed for the alias.
        if self._names_member(index, token):
            for item in sorted(self.member_names[token.text]):
                self._report(item, token)
            return
        if is_bare_use(self.tokens, index) and token.text in self.words:
            self._report(token.text, token)

    def _is_rooted_path(self, index: int) -> bool:
        """Report whether this token ends a path rooted at a selected crate.

        A ``use kani as k;`` in the file makes ``k::`` the same qualified path,
        so the qualifier is matched against the literal root and against every
        local name the file binds it to.  The check is per vocabulary: a word
        counts only for a prover that lists it, through a root that prover
        names.
        """
        if index < _QUALIFIED_SEGMENTS:
            return False
        separator = self.tokens[index - 1]
        if separator.text != "::":
            return False
        qualifier = self.tokens[index - _QUALIFIED_SEGMENTS].text
        for vocabulary in self.vocabularies:
            if self.tokens[index].text not in vocabulary.items:
                continue
            if any(qualifier in self.root_names[root] for root in vocabulary.roots):
                return True
        return False

    def _is_glob_qualified(self, index: int, token: Token) -> bool:
        """Report whether a declared module's wildcard export reaches the word.

        ``mod m { pub use kani::*; }`` makes ``m::assume`` a trust use even
        though no named binding exists, and the glob cannot be enumerated.
        Only a module this file declares is read: another qualifier names a
        module whose exports the scan never sees, and is left to rule 1 or the
        named-member rules.
        """
        return (
            token.text in self.words
            and index >= _QUALIFIED_SEGMENTS
            and self.tokens[index - 1].text == "::"
            and self.tokens[index - _QUALIFIED_SEGMENTS].text in self.module_names
        )

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
        found = (
            []
            if self.qualified is None
            else [
                (match.start(1), match[1])
                for match in self.qualified.finditer(body)
                if match[1] in self.words
            ]
        )
        found.extend(
            (match.start(), match[0])
            for match in _WORD.finditer(body)
            if match[0] in self.words
        )
        newlines = _newline_positions(token.text)
        for start, item in sorted(found):
            self._record(_literal_item(item, token, positions[start], newlines))


def _newline_positions(text: str) -> list[int]:
    """Return the offset of every newline in ``text``, in ascending order.

    Built once per literal.  Rebuilding a prefix for every match would copy the
    literal's text once per vocabulary word, which is quadratic in a
    generated literal holding many matches.
    """
    return [index for index, character in enumerate(text) if character == "\n"]


def _literal_item(
    item: str,
    token: Token,
    offset: int,
    newlines: cabc.Sequence[int],
) -> TrustItem:
    """Locate a literal match precisely enough that two of them stay distinct.

    Positions outside a literal are token positions, which the lexer already
    counts. Inside one, the token is opaque, so the offset is measured from the
    token's start and mapped through the literal's newline index.
    """
    preceding = bisect.bisect_left(newlines, offset)
    if not preceding:
        return TrustItem(item, token.line, token.column + offset)
    return TrustItem(
        item,
        token.line + preceding,
        offset - newlines[preceding - 1],
    )
