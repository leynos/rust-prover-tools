"""Prove each prover's vocabulary is read in the context that gives it meaning."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, scan
from rust_prover_tools.unittests.trust_audit_scan_support import items


@pytest.mark.parametrize(
    ("opener", "closer"),
    [("{", "}"), ("(", ")"), ("[", "]")],
)
def test_a_verus_body_reports_its_verus_vocabulary(opener: str, closer: str) -> None:
    """Read a ``verus!`` body under any delimiter pair the macro accepts."""
    source = f"verus!{opener}proof fn p() {{ admit(); }}{closer};"
    assert items(source) == [("admit", 1)]


def test_a_verus_body_does_not_report_a_kani_only_word() -> None:
    """Use each prover's own vocabulary for its own context."""
    source = "verus! { fn f() { let stub = 1; } }"
    assert items(source) == []


def test_a_renamed_kani_member_inside_a_verus_body_is_reported() -> None:
    """Apply the imported-root rule inside a ``verus!`` body too.

    The rules are cumulative: a Kani-only word in a Verus body is not Verus
    vocabulary, but it is still the item a Kani import renamed.
    """
    source = "use kani::{assume as a};\nverus! { proof fn f() { a(true); } }"
    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word inside a bare ``external`` attribute."""
    source = "#[verifier(external_body)]\nfn f() {}"
    assert items(source) == [("external_body", 1)]


def test_an_assume_termination_attribute_is_reported() -> None:
    """Report Verus' termination assumption as the request for trust it is.

    The reference calls the attribute currently unneeded, but it still tells
    the verifier to accept a termination argument without proof, so the
    inventory must name it.
    """
    source = "#[verifier::assume_termination]\nfn f() {}"
    assert items(source) == [("assume_termination", 1)]


def test_a_cfg_attr_carrying_a_verus_attribute_reports_its_arguments() -> None:
    """Report a Verus word nested inside a ``cfg_attr``."""
    source = "#[cfg_attr(verus_keep_ghost, verus_spec(admit()))]\nfn f() {}"
    assert items(source) == [("admit", 1)]


def test_a_cfg_attr_carrying_a_qualified_attribute_reports_it() -> None:
    """Keep every segment of ``verifier::external_body`` inside the region."""
    source = "#[cfg_attr(enabled, verifier::external_body)]\nfn f() {}"
    assert items(source) == [("external_body", 1)]


def test_a_cfg_attr_without_a_verus_attribute_reports_nothing() -> None:
    """Leave a ``cfg_attr`` that carries no Verus attribute alone."""
    source = "#[cfg_attr(test, should_panic)]\nfn f() {}"
    assert items(source) == []


def test_a_cfg_attr_predicate_is_not_read_as_a_verus_attribute() -> None:
    """Read only the payload: ``verifier`` in the predicate is a condition."""
    source = "#[cfg_attr(all(verifier, external), inline)]\nfn f() {}"
    assert items(source) == []


@pytest.mark.parametrize("separator", ["\f", "\v", "\u200e"])
def test_rust_whitespace_separates_a_verus_macro_head(separator: str) -> None:
    """Treat every Rust whitespace character as a token separator."""
    source = f"verus{separator}!{{ proof fn p() {{ admit(); }} }}"
    assert items(source) == [("admit", 1)]


def test_a_procedural_macro_scan_reads_literals() -> None:
    """Report the vocabulary a proc-macro crate could expand into code."""
    source = 'fn f() { let _ = "kani::assume(x); assume(y);"; }'
    assert items(source, procedural_macro=True) == [("assume", 1), ("assume", 1)]


def test_a_literal_is_not_read_without_the_procedural_macro_toggle() -> None:
    """Keep a string's contents out of the audit for an ordinary crate."""
    source = 'fn f() { let _ = "kani::assume(x);"; }'
    assert items(source) == []


def test_a_procedural_macro_scan_decodes_a_literal_escape() -> None:
    r"""Report what Rust decodes ``\x61`` to, at the escape that wrote it."""
    source = r'"kani::\x61ssume(x);"'
    found = scan(source, procedural_macro=True).items
    assert [(item.item, item.column) for item in found] == [("assume", 8)]


def test_a_procedural_macro_scan_refuses_an_undecodable_escape() -> None:
    """Fail closed rather than report a clean literal it could not read."""
    with pytest.raises(TrustAuditError, match="not a Unicode scalar value"):
        scan(r'"\u{d800}assume"', procedural_macro=True)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_line_continuation_is_not_part_of_the_literal_value(newline: str) -> None:
    r"""Skip the newline and indentation a backslash continues past.

    Rust normalizes a CRLF to LF before reading the literal, so the two
    spellings hide the newline alike.
    """
    source = f'"kani::ass\\{newline}    ume(x);"'
    found = scan(source, procedural_macro=True).items
    assert [(item.item, item.column) for item in found] == [("assume", 8)]


def test_a_literal_scan_keeps_two_occurrences_apart() -> None:
    """Locate each literal match so two in one token stay distinguishable."""
    source = '"admit(); admit();"'
    columns = [item.column for item in scan(source, procedural_macro=True).items]
    assert columns == [2, 11]


def test_a_literal_scan_reports_in_source_order() -> None:
    """Report a bare match before a qualified one that follows it."""
    source = '"assume(x); kani::assume(y);"'
    columns = [item.column for item in scan(source, procedural_macro=True).items]
    assert columns == [2, 19]


def test_an_audit_may_select_one_provers_vocabulary() -> None:
    """Apply only the vocabulary a caller selected.

    A file can import both provers.  Selecting one is how a caller asks for
    that prover's surfaces alone, and the other prover's qualified path must
    not be reported by a rule the selection left out.
    """
    source = "\n".join(
        (
            "use vstd::prelude::*;",
            "fn f() { admit(); kani::assume(true); }",
        ),
    )

    from rust_prover_tools.trust_audit.vocabulary import KANI, VERUS

    kani_only = [item.item for item in scan(source, vocabularies=(KANI,)).items]
    verus_only = [item.item for item in scan(source, vocabularies=(VERUS,)).items]
    both = [item.item for item in scan(source).items]

    assert kani_only == ["assume"]
    assert verus_only == ["admit"]
    assert both == ["admit", "assume"]


def test_a_selected_verus_vocabulary_keeps_its_region_rule() -> None:
    """A ``verus!`` body gives a selected Verus word meaning without an import."""
    from rust_prover_tools.trust_audit.vocabulary import VERUS

    source = "verus! { fn f() { admit(); } }"

    assert [item.item for item in scan(source, vocabularies=(VERUS,)).items] == [
        "admit",
    ]


def test_a_selected_kani_vocabulary_does_not_read_a_verus_region() -> None:
    """A Kani audit leaves a Verus word in a ``verus!`` body alone.

    The region rule belongs to the vocabulary record, so selecting Kani removes
    it along with the Verus words it names.
    """
    from rust_prover_tools.trust_audit.vocabulary import KANI

    source = "verus! { fn f() { admit(); } }"

    assert scan(source, vocabularies=(KANI,)).items == ()


def test_an_aliased_verus_macro_opens_a_region() -> None:
    """Treat a local name bound to ``verus`` as the same macro.

    The import reaches the macro through the crate root, so the file names no
    trust root of its own and rule 3 cannot report the body's word.  The region
    is therefore the only path that can report ``admit`` here.
    """
    source = "use crate::vstd::prelude::verus as v;\nv! { fn f() { admit(); } }"

    assert items(source) == [("admit", 2)]


def test_an_unresolved_macro_name_does_not_open_a_region() -> None:
    """Leave a body alone when the invoked name is not a known Verus macro."""
    source = "v! { fn f() { admit(); } }"

    assert items(source) == []


def test_a_attribute_value_is_not_read_as_a_verus_attribute() -> None:
    """Skip an unrelated attribute's arguments in a ``cfg_attr`` payload.

    ``doc = stringify!(verifier::external_body)`` is documentation text, not
    an applied Verus attribute.
    """
    source = (
        '#[cfg_attr(feature = "docs", doc = stringify!(verifier::external_body))]\n'
        "fn f() {}"
    )

    assert items(source) == []


def test_a_nested_cfg_attr_payload_is_still_read() -> None:
    """Recurse into a nested ``cfg_attr`` that applies a Verus attribute."""
    source = (
        '#[cfg_attr(unix, cfg_attr(feature = "x", verifier::external_body))]\nfn f() {}'
    )

    assert [item for item, _ in items(source)] == ["external_body"]


def test_a_verus_macro_alias_through_a_renamed_crate_opens_a_region() -> None:
    """Resolve a renamed Verus crate before collecting the macro alias.

    Both statements are rooted at ``crate``, so the file names no trust root
    and the region rule is again the only path that reports ``admit``.
    """
    source = "\n".join(
        (
            "use crate::vstd as local_vstd;",
            "use crate::local_vstd::prelude::verus as local_verus;",
            "local_verus! { fn f() { admit(); } }",
        ),
    )

    assert [item for item, _ in items(source)] == ["admit"]
