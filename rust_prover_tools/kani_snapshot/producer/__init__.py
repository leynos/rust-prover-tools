"""Producer-side build recipe for the Kani snapshot distribution.

This subpackage is the domain half of the producer milestone: it decides what
may be built, from which upstream identity, with which toolchain, prerequisites
and notices, and it renders that decision as a canonical document. It performs
no I/O. Workflow orchestration and command execution live in adapters outside
this package, which receive argument vectors from `commands` and a resolved
commit from `resolution`.

- `compiler_floor` owns the supported Rust floor and the transcript checks that
  demonstrate it.
- `recipe_model` holds the frozen records; `recipe_rules` owns their
  invariants; `recipe` fixes the EP-M1 values and their canonical JSON;
  `recipe_parse` reads that JSON back strictly.
- `upstream_metadata` parses the pinned upstream files the recipe must agree
  with; `resolution` selects the upstream commit a build may use.
- `commands` derives argument vectors and asset names from validated inputs.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
