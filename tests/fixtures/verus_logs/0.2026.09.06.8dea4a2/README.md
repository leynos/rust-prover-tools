# Verus log corpus for release 0.2026.09.06.8dea4a2

This directory is the oracle for `rust_prover_tools.verus_log` on Verus
`0.2026.09.06.8dea4a2`. Every `*.log` file is output captured from a real Verus
run, not a handwritten sample, and `expected.json` records the facts the parser
and the analysis must derive from it. A change to the grammar that stops
matching a real release therefore shows up as a corpus failure.

- Verus release: `0.2026.09.06.8dea4a2`, target `x86-linux`.
- Solver: `Z3 version 4.16.0 - 64 bit`, the release the repository pins
  (`Z3_VERSION` in `rust_prover_tools/binary_assets.py`).

This is the release the repository's README teaches a consumer to pin, so the
parser must accept the transcripts it prints. The newer `0.2026.11.02.8bf68ee`
corpus covers the same cases on a later release.

## What each log was captured from

Each `*.rs` file is checked in beside the logs produced from it, so a reader
can see the proof that failed and the exact wording Verus used to say so. The
capture commands below assume `VERUS` names the release's `verus` binary and
`VERUS_Z3_PATH` names the pinned solver.

| Log                     | Source              | Command                                                                       |
| ----------------------- | ------------------- | ----------------------------------------------------------------------------- |
| `verified.log`          | `verified.rs`       | `$VERUS verified.rs`                                                          |
| `postcondition.log`     | `postcondition.rs`  | `$VERUS postcondition.rs`                                                     |
| `precondition.log`      | `precondition.rs`   | `$VERUS precondition.rs`                                                      |
| `assertion.log`         | `assertion.rs`      | `$VERUS assertion.rs`                                                         |
| `two_errors.log`        | `two_errors.rs`     | `$VERUS two_errors.rs`                                                        |
| `compiler_error.log`    | `compiler_error.rs` | `$VERUS compiler_error.rs`                                                    |
| `verify_root.log`       | `verified.rs`       | `$VERUS --verify-root verified.rs`                                            |
| `selected_function.log` | `verified.rs`       | `$VERUS --verify-root --verify-function increment_value verified.rs`          |
| `usage_error.log`       | `verified.rs`       | `$VERUS --verify-function increment_value verified.rs`                        |
| `unknown_function.log`  | `verified.rs`       | `$VERUS --verify-root --verify-function absent verified.rs`                   |
| `split_streams.log`     | `postcondition.rs`  | `$VERUS postcondition.rs > stdout.txt 2> stderr.txt`, then `stdout.txt` first |

The ordinary captures merged standard error into standard output (`2>&1`), so
the diagnostic appears before the summary. `split_streams.log` is the same run
re-merged with the summary first, which proves the parser reads the same facts
from either order rather than relying on the summary coming last.

## What differs from the later release

Ten of the eleven transcripts are byte-for-byte identical to the
`0.2026.11.02.8bf68ee` corpus. The exception is `usage_error.log`: this release
prints the driver's option error as

```text
Error: --verify-function option requires --verify-only-module or --verify-root
```

where the later release prints
``error: `--verify-function` option requires
`--verify-only-module` or `--verify-root` ``.
The grammar accepts both spellings, and this corpus is why: before it was
added, the capitalized line was skipped as chatter and the run was reported as
a tool error with no reason.

## Regenerating the corpus

Capture into this directory with the release and solver named above, then keep
both streams: a capture missing standard error loses every diagnostic, and one
missing standard output loses the summary. Update `expected.json` from the new
output only after checking that each recorded fact still describes what Verus
printed. Adding a release means adding a sibling version directory and listing
it in `SUPPORTED_VERUS_VERSIONS`.

The corpus was captured on Linux x86-64. Paths appear exactly as Verus echoed
them: relative to the directory each command ran in, which is why they name a
file rather than an absolute location.

## Licence

The `*.rs` sources in this directory were written for this corpus, and the
transcripts are Verus output from those sources. Both are distributed under
this repository's ISC licence; see `LICENSE` at the repository root.
