# rust-prover-tools 0.2.0 migration guide

Version 0.2.0 makes `verus run` verify more than one proof file in a single
command. The change is additive for command-line users and breaking only for
callers that construct `VerusRunOptions` directly.

## Command-line users

`--proof-file` may now be repeated. Every named file is verified, in the order
given, in its own Verus invocation, because the verifier rejects more than one
input filename per run:

```bash
prover-tools verus run --repo-root . \
  --proof-file verus/edge_harvest_proofs.rs \
  --proof-file verus/session_core_distance.rs
```

A single `--proof-file`, or none at all, behaves as before, including the
`verus/edge_harvest_proofs.rs` compatibility default.

The run stops at the first failing file. The error message keeps the failing
path on its `Proof file:` line and lists the files already verified on a
`Proved before the failure:` line, and the process exits with that verifier's
status. No command-line change is required to adopt 0.2.0.

`VERUS_PROOF_FILE` and `INPUT_PROOF_FILE` remain single-file bindings. A path
separator inside one of those values is part of the path, not a delimiter
between proof files. Repeating `--proof-file` on the command line overrides the
environment value.

## Python API callers

`VerusRunOptions.proof_file` is replaced by `VerusRunOptions.proof_files`, a
tuple verified in order. An empty tuple selects the compatibility default.

The former single-path form still works during the deprecation period: the
`proof_file=` keyword, and a `Path` in the former second positional slot, are
folded into `proof_files`. Passing both forms raises `TypeError`, so there is
only ever one source of truth.

`ProofFailureContext` gains `proved_proof_files`, the files verified before the
failure, in the order they ran. It is empty for a single-file run or a failure
on the first file.

Callers that unpack `VerusRunOptions` positionally past the first argument
should switch to keyword arguments, because `proof_files` now occupies the
second slot in the constructor and the later fields keep their previous
positions only for keyword use.

## Why the API changed

The predecessor shell wrapper iterated a proof-file array. Verus itself accepts
only one input filename per invocation, so the Python API now expresses the
list and the runner performs one invocation per entry. A manifest or argfile
was rejected: a repeatable flag covers the original list without inventing a
file format.
