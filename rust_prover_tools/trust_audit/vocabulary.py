"""Name the vocabulary a proof uses to ask for acceptance without argument.

The audit answers one question about a proof: *what did the author tell the
verifier to accept without proof?* Each prover answers it with its own words,
so each is described by a frozen :class:`Vocabulary` record rather than read
from a configuration file: a word joins a record when the release this package
pins documents it, and a release that adds one changes this module.

A record is data, not behaviour.  It names the words, the crate roots that
qualify them, and whether a ``verus!`` body or attribute gives them meaning;
`rust_prover_tools.trust_audit.matcher` holds the rules that read a record.
Because the matcher takes the records it is given, a caller can select the
provers it cares about, and a later user-supplied vocabulary can be passed in
the same shape without changing the matcher.

Both sets are matched by context rather than by spelling, because the same
words are ordinary Rust elsewhere: ``assume`` is also a plausible identifier,
``should_panic`` is a standard test attribute, and ``external`` appears in prose
and in paths.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "KANI",
    "KANI_ROOT",
    "KANI_VOCABULARY",
    "OPAQUE_MACROS",
    "PROCEDURAL_OPAQUE_MACROS",
    "PROVER_VOCABULARIES",
    "TRUST_ROOTS",
    "VERUS",
    "VERUS_ATTRIBUTE_HEADS",
    "VERUS_ROOTS",
    "VERUS_VOCABULARY",
    "VOCABULARY",
    "Vocabulary",
    "select_vocabularies",
)


@dc.dataclass(frozen=True, slots=True)
class Vocabulary:
    """One prover's trust vocabulary and the context that gives it meaning.

    Attributes
    ----------
    prover : str
        The name a caller selects this record by.
    items : frozenset[str]
        The words this prover documents as requests for trust.
    roots : frozenset[str]
        The crate roots a qualified path may start from.  A path rooted at one
        of them names this prover's surface, and a file importing one of them
        makes a bare vocabulary word ambiguous.
    regions : bool
        Whether a ``verus!`` body or a Verus verification attribute gives the
        words meaning without a path or a bare import.  Only Verus has such a
        wrapper, so the record states the fact rather than the matcher knowing
        the prover's name.
    """

    prover: str
    items: frozenset[str]
    roots: frozenset[str]
    regions: bool = False


#: Kani's surfaces: an assumption, an unconstrained value, two kinds of stub,
#: and the attribute that declares a harness must panic.  A ``kani::`` path is
#: what makes one of them trust at the use site.
KANI = Vocabulary(
    prover="kani",
    items=frozenset(
        {
            "assume",
            "any_where",
            "stub",
            "stub_verified",
            "should_panic",
        },
    ),
    roots=frozenset({"kani"}),
)

#: Verus's surfaces: assumptions, the admission of goals, and the external-item
#: escape hatches that supply a body or a specification from outside.  Verus
#: words are trust inside a ``verus!`` body or attribute, or in a file that
#: imports from ``vstd`` or ``verus_builtin``.
VERUS = Vocabulary(
    prover="verus",
    items=frozenset(
        {
            "assume",
            "admit",
            "external_body",
            "external",
            "external_fn_specification",
            "assume_specification",
            "external_type_specification",
            "external_trait_specification",
            "exec_allows_no_decreases_clause",
            "assume_termination",
        },
    ),
    roots=frozenset({"vstd", "verus_builtin"}),
    regions=True,
)

#: Every vocabulary the audit knows, in the order a full report reads them.
PROVER_VOCABULARIES: typ.Final[tuple[Vocabulary, ...]] = (KANI, VERUS)

#: Kani's words, for callers that work with one prover's set directly.
KANI_VOCABULARY: typ.Final[frozenset[str]] = KANI.items

#: Verus's words, for callers that work with one prover's set directly.
VERUS_VOCABULARY: typ.Final[frozenset[str]] = VERUS.items

#: Every word either prover recognizes as a request for trust.
VOCABULARY: typ.Final[frozenset[str]] = frozenset(
    item for vocabulary in PROVER_VOCABULARIES for item in vocabulary.items
)

#: The crate a qualified Kani path is rooted at.
KANI_ROOT: typ.Final[str] = "kani"

#: The crates a qualified Verus path is rooted at.
VERUS_ROOTS: typ.Final[frozenset[str]] = VERUS.roots

#: Every crate root whose import makes a bare vocabulary word ambiguous.
TRUST_ROOTS: typ.Final[frozenset[str]] = frozenset(
    root for vocabulary in PROVER_VOCABULARIES for root in vocabulary.roots
)

#: Attribute heads that make their whole attribute list Verus vocabulary.
VERUS_ATTRIBUTE_HEADS: typ.Final[frozenset[str]] = frozenset(
    {"verifier", "verus_verify", "verus_spec"},
)

#: Macros that generate or splice code the audit never sees.
OPAQUE_MACROS: typ.Final[frozenset[str]] = frozenset(
    {"include", "paste", "concat_idents"},
)

#: A macro whose splice is opaque only where literals are read as code.
#:
#: ``include_str!`` expands to a string literal and ``include_bytes!`` to a
#: byte array.  An ordinary crate's literals are trivia, so either expansion
#: adds nothing the audit reads.  A proc-macro crate's literals are scanned as
#: potential code, and such a crate can decode a spliced template and parse it
#: into a token stream, so there both are as opaque as ``include!``.
PROCEDURAL_OPAQUE_MACROS: typ.Final[frozenset[str]] = frozenset(
    {"include_str", "include_bytes"},
)


def select_vocabularies(names: cabc.Iterable[str] | None) -> tuple[Vocabulary, ...]:
    """Return the records a caller selected, or every record when unnamed.

    Parameters
    ----------
    names : Iterable[str] | None
        Prover names to select.  ``None`` or an empty collection selects every
        known vocabulary, which is the audit's default: reporting both provers
        is the fail-closed direction, and a caller narrows it deliberately.

    Returns
    -------
    tuple[Vocabulary, ...]
        The selected records, in the order they were first named.

    Raises
    ------
    ValueError
        If a name is not a known prover, because reporting silently for a name
        the audit does not recognize would hide the mistake.
    """
    if names is None:
        return PROVER_VOCABULARIES
    by_name = {vocabulary.prover: vocabulary for vocabulary in PROVER_VOCABULARIES}
    selected: list[Vocabulary] = []
    for name in names:
        vocabulary = by_name.get(name)
        if vocabulary is None:
            known = ", ".join(sorted(by_name))
            msg = f"unknown prover {name!r}; known provers are {known}"
            raise ValueError(msg)
        if vocabulary not in selected:
            selected.append(vocabulary)
    return tuple(selected) or PROVER_VOCABULARIES
