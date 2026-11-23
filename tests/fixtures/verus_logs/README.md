# Verus log corpus

Each subdirectory is one supported Verus release and holds the real transcripts
the parser is qualified against:

```text
tests/fixtures/verus_logs/<release>/
  README.md       what was captured, from which sources, with which solver
  expected.json   the facts every log must yield and the decision it must reach
  *.rs            the source each transcript was produced from
  *.log           the captured output, never hand-edited
```

A release joins `SUPPORTED_VERUS_VERSIONS` only together with its own corpus
directory. The grammar is derived from these transcripts, so a change that
stops matching a real release fails the corpus tests instead of changing what
an analysis silently reports.

The corpus is captured with the solver the package pins, `Z3 4.16.0`. Verus
writes its `verification results::` summary to standard output and diagnostics
to standard error; capture both, because a log missing either loses facts the
analysis needs.

To add a release:

1. Copy the sources from the newest existing directory, or add a case that the
   new release renders differently.
2. Capture every case with that release into a release-named directory.
3. Record the facts in `expected.json` and check each one against the captured
   output. Do not derive the oracle from the parser's own answer.
4. Add the release to `SUPPORTED_VERUS_VERSIONS`.
5. Write the directory's `README.md` with the release, solver, commands and any
   wording that differs from the other captures.

Two releases are qualified today: `0.2026.11.02.8bf68ee`, the newest capture,
and `0.2026.09.06.8dea4a2`, the release the repository README teaches a
consumer to pin. Their transcripts agree except for the driver's option-error
spelling, which the grammar accepts in both forms.

The sources are distributed under this repository's ISC licence; see `LICENSE`
at the repository root.
