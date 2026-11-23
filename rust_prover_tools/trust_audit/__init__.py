"""Report what a Rust proof asked its verifier to accept without proof.

A trust audit answers one question about a proof: *where did the author tell
the verifier to stop checking?* The answer is a list of items and locations,
one per request, so a reviewer can read each against a justification:

- :mod:`.vocabulary` names the words each prover documents for such a request.
- :mod:`.syntax` lexes Rust far enough that a word inside a comment, string or
  character literal is never read as code; :mod:`.cursor` and :mod:`.literals`
  hold the position arithmetic and the literal forms it shares with the
  decoder.
- :mod:`.structure`, :mod:`.conditions`, :mod:`.macro_rules`,
  :mod:`.inline_modules`, :mod:`.module_paths`, :mod:`.use_tree`,
  :mod:`.use_paths`, :mod:`.crate_bindings`, :mod:`.use_bindings`,
  :mod:`.use_alias`, :mod:`.manifests` and :mod:`.modules` locate the
  brackets, conditional attributes, macro invocations, inline chains, module
  paths, imports, bound names, crate kinds and module declarations the rules
  depend on, and :mod:`.regions` locates the ``verus!`` bodies and attributes.
- :mod:`.context` applies the rules to one file's tokens.
- :mod:`.matcher` decides which occurrences of the vocabulary are trust.
- :mod:`.tree` follows ``mod`` declarations from a set of entry points, so the
  audit covers compiled code and not stray files.

The walk needs a filesystem.  :mod:`.drivers` and :mod:`.targets` expand the
command's arguments into Cargo targets, :mod:`.manifests` reads the crate kind,
and :mod:`.tree` reads the sources and follows their declarations; those four
rooms are the package's I/O edge.  Every other module is a pure function of the
text it is given.

Examples
--------
>>> scan("fn f() { kani::assume(true); }").items
(TrustItem(item='assume', line=1, column=16),)
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.drivers import expand_drivers
from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.matcher import scan
from rust_prover_tools.trust_audit.model import (
    AuditedFile,
    InlineModule,
    ModuleReference,
    ScanResult,
    TrustItem,
)
from rust_prover_tools.trust_audit.tree import RUST_SUFFIX, audit_tree
from rust_prover_tools.trust_audit.vocabulary import (
    PROVER_VOCABULARIES,
    Vocabulary,
    select_vocabularies,
)

__all__ = (
    "PROVER_VOCABULARIES",
    "RUST_SUFFIX",
    "AuditedFile",
    "InlineModule",
    "ModuleReference",
    "ScanResult",
    "TrustAuditError",
    "TrustItem",
    "Vocabulary",
    "audit_tree",
    "expand_drivers",
    "scan",
    "select_vocabularies",
)
