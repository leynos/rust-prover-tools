"""Guarded producer workflow for the Kani snapshot distribution.

This subpackage is the domain half of the producer milestone: it decides what
may be built, from which upstream identity, with which toolchain, prerequisites
and notices, and it renders that decision as a canonical document.

- `compiler_floor` owns the supported Rust floor and the transcript checks that
  demonstrate it.
- `recipe_model` holds the frozen records; `recipe_rules` owns their
  invariants; `recipe` fixes the EP-M1 values and their canonical JSON;
  `recipe_parse` reads that JSON back strictly.
- `upstream_metadata` parses the pinned upstream files the recipe must agree
  with; `resolution` selects the upstream commit a build may use.
- `commands` derives argument vectors and asset names from validated inputs.

The guarded job lifecycle sits beside the recipe. `lifecycle_model` holds the
records a job hands to the next one and `lifecycle` renders and parses them;
`assemble_input` and `assemble` turn measured payloads into the manifest and
checksums; `validation_receipt` records the clean-validation evidence; and
`entrypoint` is the only command line a workflow job runs. Those modules read
and write files, but the subpackage still never executes the build itself and
never calls the release API: `rust_prover_tools.kani_snapshot.publish` owns
publication, and the workflow supplies the platform it runs on.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
