"""Guarded producer policy for the Kani snapshot distribution.

This subpackage is the domain half of the producer: it decides what may be
built, from which upstream identity, with which toolchain, prerequisites and
notices, and it renders that decision as a canonical document. It performs no
effect of its own — no file is read or written, no process, environment or
network call is made — so everything here is testable from literal values.

- `compiler_floor` owns the supported Rust floor and the transcript checks that
  demonstrate it.
- `recipe_model` holds the frozen records; `recipe_rules` owns their
  invariants; `recipe` fixes the EP-M1 values and their canonical JSON;
  `recipe_parse` reads that JSON back strictly.
- `upstream_metadata` parses the pinned upstream files the recipe must agree
  with; `resolution` selects the upstream commit a build may use, from the
  shapes `candidate_api` and `upstream_evidence` project.
- `commands` derives argument vectors and asset names from validated inputs;
  `asset_names` derives the release's expected asset set.
- `assemble_input`, `evidence_model` and `lifecycle_model` hold the records a
  job hands to the next one; `lifecycle`, `assemble_input`'s codec,
  `publication_document`, `publication_identity` and `validation_receipt`
  render and strictly parse them.
- `input_fingerprint` derives the build-input digest that identity allocation
  and publication compare.

Carrying those decisions out — reading a document, writing one, fetching from
GitHub — belongs to `rust_prover_tools.kani_snapshot.orchestrator`, which is
the only layer a workflow job invokes and this package never imports.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
