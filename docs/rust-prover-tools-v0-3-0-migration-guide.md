# rust-prover-tools 0.3.0 migration guide

This guide describes the additive changes an existing user needs to know about
in the next minor release after rust-prover-tools 0.2.0. The package version in
`pyproject.toml` is updated when that release is cut; the commands below are
already present on the main branch. No existing command changes, so a caller
that does not adopt the new analysis commands needs no edit. Readers new to the
tool should start with the [users' guide](users-guide.md).

## What changed

Three commands report what a captured verifier run proved, without needing the
verifier installed at analysis time:

- `prover-tools kani analyse-log` reads a captured `cargo kani --verbose`
  transcript and reports one neutral outcome per manifest claim. See
  [Kani log analysis](users-guide.md#kani-log-analysis).
- `prover-tools verus analyse-log` reads a captured Verus transcript and reports
  what the run proved, including a declared negative-control region. See
  [Verus log analysis](users-guide.md#verus-log-analysis).
- `prover-tools audit trust` lists the places a proof asked Kani or Verus to
  accept something without proof. See [Trust audit](users-guide.md#trust-audit).

The Verus parser is a new design rather than a port. The Kani parser and trust
audit are adopted from combobulate and record their grammar decisions in
[the CLI design](prover-tools-cli-design.md#log-analysis-and-trust-audit-adoption).

## What a caller needs to do

Nothing, unless the caller wants the new reports. The analysis commands are
additive: they read files and print a report, and they do not change the exit
status of `kani exec` or `verus run`. A caller that already captures verifier
output can pass that file to the matching `analyse-log` command.

`verus run` qualifies the selected solver on every run, whether the selection
comes from `--z3` or from `VERUS_Z3_PATH`. A caller that exports
`VERUS_Z3_PATH` must point it at the solver installed by
`prover-tools z3 install`, or at another solver whose banner reports the
calibrated Z3 release; a different release is refused before the first proof
file runs.

A captured log is qualified against a pinned release when one is named or found.
`kani analyse-log` accepts Kani 0.67.0, and `verus analyse-log` accepts the
releases named by `verus analyse-log --help` and the corpus under
`tests/fixtures/verus_logs/`. Name the release with `--verus-release`, or run
in a repository whose `tools/verus/VERSION` pin records it. When neither names
a release, Verus analysis proceeds with an unnamed release and says so rather
than guessing one.

## Compatibility

- The existing `kani exec`, `verus run` and `z3` commands keep their options
  and exit codes.
- A selected solver continues to be exported as `VERUS_Z3_PATH` for each
  invocation; `verus run --z3` now also reports the qualified solver before the
  first proof file runs.
- A manifest, transcript or source the analysis cannot read is refused rather
  than reported as a clean result. The exit status is `1` for unreadable input
  and `2` for a completed analysis that did not pass.
