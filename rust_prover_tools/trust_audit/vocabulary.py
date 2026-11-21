"""Name the vocabulary a proof uses to ask for acceptance without argument.

The audit answers one question about a proof: *what did the author tell the
verifier to accept without proof?* Each prover answers it with its own words,
so the sets below are defined by the provers rather than read from a
configuration file: a word joins a set when the release this package pins
documents it, and a release that adds one changes this module.

Both sets are matched by context rather than by spelling, because the same
words are ordinary Rust elsewhere: ``assume`` is also a plausible identifier,
``should_panic`` is a standard test attribute, and ``external`` appears in prose
and in paths. `rust_prover_tools.trust_audit.matcher` holds the rules that give
a word meaning; this module only names the words.
"""

from __future__ import annotations

import typing as typ

__all__ = (
    "KANI_ROOT",
    "KANI_VOCABULARY",
    "OPAQUE_MACROS",
    "PROCEDURAL_OPAQUE_MACROS",
    "TRUST_ROOTS",
    "VERUS_ATTRIBUTE_HEADS",
    "VERUS_ROOTS",
    "VERUS_VOCABULARY",
    "VOCABULARY",
)

#: Kani's surfaces: an assumption, an unconstrained value, two kinds of stub,
#: and the attribute that declares a harness must panic.
KANI_VOCABULARY: typ.Final[frozenset[str]] = frozenset(
    {
        "assume",
        "any_where",
        "stub",
        "stub_verified",
        "should_panic",
    },
)

#: Verus's surfaces: assumptions, the admission of goals, and the external-item
#: escape hatches that supply a body or a specification from outside.
VERUS_VOCABULARY: typ.Final[frozenset[str]] = frozenset(
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
)

#: Every word either prover recognizes as a request for trust.
VOCABULARY: typ.Final[frozenset[str]] = KANI_VOCABULARY | VERUS_VOCABULARY

#: The crate a qualified Kani path is rooted at.
KANI_ROOT: typ.Final[str] = "kani"

#: The crates a qualified Verus path is rooted at.
VERUS_ROOTS: typ.Final[frozenset[str]] = frozenset({"vstd", "verus_builtin"})

#: Every crate root whose import makes a bare vocabulary word ambiguous.
TRUST_ROOTS: typ.Final[frozenset[str]] = VERUS_ROOTS | {KANI_ROOT}

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
#: ``include_str!`` expands to a string literal.  An ordinary crate's literals
#: are trivia, so the expansion adds nothing the audit reads.  A proc-macro
#: crate's literals are scanned as potential code, and a spliced file's text
#: never passed through the lexer, so there the macro is as opaque as
#: ``include!``.
PROCEDURAL_OPAQUE_MACROS: typ.Final[frozenset[str]] = frozenset({"include_str"})
