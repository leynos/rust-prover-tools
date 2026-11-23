# Kani 0.67.0 log corpus

This directory holds the real Kani 0.67.0 transcripts the parser is qualified
against. `expected.json` is the parser oracle: it records the facts every log
must yield and the decision each must reach at the time of capture.
`decision_table.json` holds the precedence table as data,
`corpus-manifest.json` declares every harness in the corpus crate, and
`in_place-manifest.json` is the standalone harness manifest for the
probe-generated `in_place` run.

The logs are corpus evidence. Do not hand-edit them. Recapture a case when a
new Kani release is qualified, and add a version-named corpus for that release
before it joins `SUPPORTED_KANI_VERSIONS`. Capture the complete standard output
followed by the complete standard error, in that order. The corpus below was
captured from the direct driver; a future capture through the CLI takes the
same shape:

```bash
prover-tools kani exec --installation "$HOME/.kani/kani-0.67.0" --cwd "$PWD" \
  --extra-arg=--verbose --extra-arg=--harness=harnesses::verified \
  >kani.out 2>kani.err
cat kani.out kani.err >kani.log
```

Interleaving the streams with `2>&1` can split a report block, because the
compiler build chatter and the driver's report share both streams. The parser
refuses a log whose blocks are split rather than guessing a verdict.

`crate/` holds the corpus crate sources, `Cargo.lock`, `harnesses.json` and the
captured `kani-list.json`, so a maintainer can recapture these logs from the
recorded commit. `kani-list.json` is an inventory only; it cannot express a
claim or control role, a cover, an expected failure or `should_panic`.

## Capture details

- Kani: `0.67.0`
- Rust toolchain: `nightly-2025-11-21-x86_64-unknown-linux-gnu`
- Route: direct `kani-driver` invocation, not `cargo kani`
- Qualification: this corpus qualifies the direct-driver output. The
  `cargo kani` wrapper's banner and build chatter are accepted by the grammar,
  but that route is not independently captured here.
- Working directory: `tools/verifier-fixtures/kani-corpus` in the origin
  repository
- Source crate: `kani-corpus` at origin commit
  `9dd6e3c24306ee947ca3bf3bc76339cd67509f1d`
- Stream order: captured standard output followed by captured standard error

The `list` log is `kani list --format json` output and is an inventory, not a
verification run. The `truncated` log was killed mid-run to capture a
front-to-back incomplete report; it must be refused.

| Log                          | Exit | Capture command                                                                                                                                    |
| ---------------------------- | ---: | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verified.log`               | 0    | `kani --harness harnesses::verified --exact --verbose`                                                                                             |
| `counterexample.log`         | 1    | `kani --harness harnesses::counterexample --exact --verbose`                                                                                       |
| `unsatisfied_cover.log`      | 0    | `kani --harness harnesses::unsatisfied_cover --exact --verbose`                                                                                    |
| `multiplication_timeout.log` | 1    | `kani --harness harnesses::multiplication_timeout --exact --verbose -Z unstable-options --harness-timeout 5s`                                      |
| `insufficient_unwind.log`    | 1    | `kani --harness harnesses::insufficient_unwind --exact --verbose`                                                                                  |
| `vacuous_assumption.log`     | 0    | `kani --harness harnesses::vacuous_assumption --exact --verbose`                                                                                   |
| `unsupported.log`            | 1    | `kani --harness harnesses::unsupported --exact --verbose`                                                                                          |
| `expected_panic.log`         | 0    | `kani --harness harnesses::expected_panic --exact --verbose`                                                                                       |
| `seeded_wrapping_mutant.log` | 1    | `kani --harness harnesses::seeded_wrapping_mutant --exact --verbose`                                                                               |
| `selected_harness.log`       | 0    | `kani --harness harnesses::verified --exact --verbose`                                                                                             |
| `fail_fast.log`              | 1    | `kani --harness harnesses::counterexample --harness harnesses::verified --harness harnesses::seeded_wrapping_mutant --exact --fail-fast --verbose` |
| `truncated.log`              | 124  | `timeout --signal=TERM 8s kani --harness harnesses::multiplication_timeout --exact --verbose`                                                      |
| `list.log`                   | 0    | `kani list --format json`                                                                                                                          |
| `in_place.log`               | 0    | `kani --harness verification::target_facts --exact --verbose`                                                                                      |
| `default_unwind.log`         | 1    | `kani --harness harnesses::verified --harness harnesses::seeded_wrapping_mutant --exact --verbose --default-unwind 2`                              |

The commands were run through the driver at
`~/.kani/kani-0.67.0/bin/kani-driver`. The harness names and source paths are
recorded exactly as the logs printed them, including the origin worktree's
absolute paths in build chatter. The parser reads only report structure, so
those host paths do not affect the oracle.

## Licence

The transcripts were captured from the combobulate repository and are
redistributed here under its ISC licence:

```text
ISC License

Copyright © 2026 Payton McIntosh <pmcintosh@df12.net>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```
