"""The effects the producer's decisions are carried out by.

`rust_prover_tools.kani_snapshot.producer` decides what may be built, from
which upstream identity, and renders the documents a job hands to the next one.
This package is the other half: every command a workflow job runs, and every
read or write of a path it performs. It holds no policy of its own, so a step
here parses a document, calls one producer function and records the result.

The split is by effect rather than by data shape, which is what lets the
producer package be tested without a filesystem and this package be tested
against temporary directories and injected transports:

- `entrypoint` is the only command line a workflow job runs; `operations` and
  `resolve_operations` implement its subcommands.
- `assembly`, `release_evidence`, `measure_build`, `build_evidence`,
  `inventory_build` and `documents` read and write the release's files.
- `collect_candidates` and `collect_releases` perform the bounded `gh` reads
  and project their responses through the producer's pure projections.

`documents` owns the one atomic writer, so a reader never sees a partial
document and the producer never needs a filesystem.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
