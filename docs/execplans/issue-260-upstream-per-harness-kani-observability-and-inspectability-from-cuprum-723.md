# Upstream per-harness Kani observability and inspectability from Cuprum #723

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS (implementation complete; draft PR #266 open; nine local
review passes addressed, residual skips documented)

Branch:
`issue-260-upstream-per-harness-kani-observability-and-inspectability-from-cuprum-723`

Roadmap item: `2.1` in `docs/roadmap.md` (added by this plan).

## Purpose / big picture

Cuprum's `extended (kani)` job has been killed with exit 143 (SIGTERM) about
thirteen minutes into SSA conversion since 26 October 2026, although the job
timeout is 45 minutes. The job log is the only evidence that survives such a
runner loss, and today that log cannot say which harness was running, how much
memory it used, or whether the host was exhausted. The source plan in
[leynos/cuprum#723](https://github.com/leynos/cuprum/issues/723) therefore asks
for a per-harness Kani driver with an inspectable inventory, supervised
execution, live evidence, memory sampling, watchdogs, typed outcomes, and run
reports. This repository is the right home for that machinery: it already owns
Cuprum-backed execution, the qualified Kani release and snapshot routes, and
opt-in structured telemetry.

After this change a library consumer can discover every Kani harness in a Cargo
workspace, inspect the exact package-qualified inventory before running
anything, and execute each harness in its own supervised invocation. While a
harness runs, the caller sees flushed start records, timestamped child output,
periodic heartbeats, and resource samples in both a human job log and an
incrementally flushed JSONL stream. When a harness exceeds its wall-clock
budget, or host available memory crosses a configured threshold, the supervisor
stops that harness's process group and records an attributable outcome instead
of losing the runner. When the run ends, the caller receives typed results, a
deterministic table, and a versioned JSON report that state the sampled peak
process-group RSS, the minimum host available memory, the cgroup observations,
and the termination evidence. Missing, duplicate, or unexpected executions fail
the aggregate result.

The same capability is reachable from the command line as
`prover-tools kani harnesses list` and `prover-tools kani harnesses run`. Kani
0.67.0 is the only registered integration, but the supervision, measurement,
accounting, and reporting layers are prover-neutral so another adapter can be
added without copying them.

Plain-language terms used in this plan:

- **Harness**: one Kani proof entry point, marked with `#[kani::proof]` or
  `#[kani::proof_for_contract]`, whose fully qualified Kani *pretty name* is
  the selector used with `--harness`.
- **Inventory**: the resolved, package-qualified list of harnesses that a run
  is expected to execute.
- **Multiset accounting**: comparing the expected and executed identities as
  multisets, so a missing, duplicated, or unexpected execution is an error even
  when the total count matches.
- **Measurement**: either an available integer byte count or an explicit
  `Unavailable` reason; absence is data, not an exception.
- **Stop reason**: why the supervisor ended a child (`exited`, `timeout`,
  `memory-watchdog`, `interrupted`, or `launch-failed`), kept separate from the
  proof verdict and the raw exit status.

## Constraints

- The supported Kani grammar is exactly 0.67.0. A banner naming any other
  version raises `UnsupportedKaniGrammarError`; the parser never guesses at a
  release it was not written for.
- The existing `kani exec` and `kani snapshot run` behaviour must remain
  byte-identical: same stage events, same environment, same argv, same exit
  status. The new routes compose with them; they do not replace them.
- `rust_prover_tools/kani_snapshot/producer/compiler_floor.py` must not change.
- `rust_prover_tools/observability.py` keeps its bounded telemetry contract:
  no paths, URLs, argv, digests, prover output, run identifiers, or harness
  identifiers may become metric labels. Harness and run identifiers belong in
  the explicit diagnostic sinks.
- The library installs no logging handlers and no signal handlers. Signal
  handling lives only in the CLI module.
- The baseline Linux path must work without `sudo` and without mandatory
  cgroup control. Every optional cgroup read is fail-open: an unreadable file
  must not prevent a proof from running.
- Do not add unwind overrides, disable unwinding assertions, narrow proof
  domains, or otherwise change proof meaning. Proof-affecting arguments are
  preserved verbatim.
- No Cuprum inventory, repository layout, output directory, or harness name
  may be hard-coded. Packages and paths are inputs.
- Every Python module stays within the repository's 400-line pylint limit and
  the configured complexity limits (`pyproject.toml`, `[tool.pylint.*]`). Every
  definition under `rust_prover_tools` and `tests` needs a NumPy-style docstring
  (`make interrogate`).
- Markdown follows `docs/documentation-style-guide.md`: en-GB-oxendict
  spelling, sentence-case headings, prose wrapped at 80 columns, code wrapped
  at 120 columns, captioned tables and diagrams.
- Only 0.67.0 is registered. Adding a version requires a captured corpus of a
  passing harness, a counterexample, a no-match run, and a truncated run.
- Implementation proceeds on the user's explicit instruction to implement the
  approved coding plan; no further approval gate applies to this task.

## Tolerances (exception triggers)

- Files: if the implementation needs more than 60 non-ExecPlan files, stop and
  escalate.
- Code: if net lines of production source plus tests exceed 9,000, stop and
  escalate. The estimate is based on the ported parser (about 1,200 lines with
  tests and corpus), the supervision and measurement layer (about 3,000), the
  inventory layer (about 1,500), the route and CLI integration (about 1,500),
  and fixtures (about 1,500).
- Configuration: if net lines of configuration change exceed 100, stop and
  escalate. Only `pyproject.toml` and CI-facing configuration may need small
  changes; no new runtime dependency is planned.
- Documentation: if net lines of documentation excluding ExecPlan files exceed
  1,200, stop and escalate.
- Dependencies: no new runtime or development dependency is authorized. If a
  new dependency seems necessary, stop and escalate.
- Architecture: if the work needs to change the process-group ownership
  pattern, the qualified route contracts, or the bounded telemetry contract,
  stop and escalate.
- Iterations: if a focused gate still fails after five distinct fix attempts,
  stop and escalate with the failing log.
- Ambiguity: if a required behaviour admits two materially different
  interpretations, stop and present the options.
- ExecPlan files themselves are excluded from all size counting.

## Risks

- Risk: Cuprum's `run()` forces capture on its owned-handle path, and a verbose
  proof log could exhaust memory. Severity: high. Likelihood: medium.
  Mitigation: use `SafeCmd.run()` with `RunOutputOptions(capture=False)` and a
  bounded `on_line` consumer; never use `SafeCmd.start()`, whose handle forces
  capture.
- Risk: the async monitor loop may race the launcher task.
  Severity: medium. Likelihood: medium. Mitigation: a single monitor task owns
  the launcher task and every stop decision; every stop awaits the launcher's
  completion before classifying.
- Risk: process-group RSS sampling may double-count shared pages, miss spikes
  between samples, and include compilation. Severity: medium. Likelihood: high.
  Mitigation: document the limitations in the report, the users' guide, and the
  schema document; never present the sample as an exclusive peak.
- Risk: a host with no readable cgroup v2 files or no `memory.peak`.
  Severity: low. Likelihood: high. Mitigation: `Unavailable(reason)` values;
  tests cover missing and unreadable files.
- Risk: the lexical Rust scanner misclassifies comments, raw strings, or
  lifetimes. Severity: medium. Likelihood: medium. Mitigation: a small explicit
  scanner with tests for each lexical form, plus fixture workspaces covering
  `#[path]`, inline modules, and `cfg`-gated items.
- Risk: the parser's incremental state machine could disagree with the
  whole-text port. Severity: high. Likelihood: medium. Mitigation: implement
  whole-text parsing as feed-line plus finish, and add a line-fed equivalence
  property test over every corpus log and generated chatter.
- Risk: a newline-free verbose line can accumulate in Cuprum before the
  bounded hook receives it. Severity: high. Likelihood: low. Mitigation:
  document the residual upstream limit, bound every line and sink the
  supervisor receives, and ensure the classifier never needs the full line. The
  incomplete-line buffer is not claimed to be bounded.
- Risk: CodeRabbit's shared review allowance may be exhausted.
  Severity: low. Likelihood: medium. Mitigation: run the deterministic gates
  before each review request; wait with `vsleep` as instructed on rate limits.

## Progress

- [x] (2026-11-21 11:35Z) Reconnaissance: read `commands.py`,
  `observability.py`, `kani_release.py`, `kani_snapshot/run.py`, the CLI,
  `pyproject.toml`, the tests, and the Cuprum 0.2.0b2 public API.
- [x] (2026-11-21 11:40Z) Ported-source reconnaissance: read the Combobulate
  `kani_log` package and its corpus at commit `9dd6e3c`.
- [x] (2026-11-21 11:45Z) Confirmed the installed Kani 0.67.0 help output:
  `--output-format` defaults to `regular` and accepts `regular`, `terse`, and
  `old`; `--exact` matches the fully qualified name.
- [x] (2026-11-21 11:50Z) Branch renamed and Lody session titled.
- [x] (2026-11-21 12:40Z) `EP-M1` Kani 0.67.0 grammar, incremental reader,
  registry, corpus, and typed timeout reason. All fifteen corpus logs parse or
  refuse exactly as committed; `make all`, Markdown lint, and Mermaid
  validation pass.
- [x] (2026-11-21 14:20Z) `EP-M2` Prover-neutral supervision models,
  measurement, evidence sinks, accounting, report schema, and runner. The
  deterministic suite covers timeouts, watchdog stops, first-stop precedence,
  continuation, interruption, unavailable cgroup and host values, and the
  versioned schema; one real-process witness proves group cancellation and
  heartbeat emission. `make all` passes with 1,661 tests.
- [x] (2026-11-21 15:40Z) `EP-M3` Package-qualified Kani inventory
  discovery, strict inventory JSON, exact selection, and argv composition. The
  two-package fixture workspace covers `#[path]`, inline modules, all three
  target kinds, a conditional proof, an empty package, an ambiguous module, a
  package with no verifiable target, and a duplicate pretty name across targets.
  `make all PYTEST_XDIST_WORKERS=4` passes with 1,727 tests.
- [x] (2026-11-21 17:05Z) `EP-M4` Qualify-once route seams, Kani
  classification, public typed API, and supervised entry point. The legacy and
  snapshot routes expose one-time qualification and derive every harness
  command from it; `KaniAdapter` applies the documented precedence; and
  `run_harnesses` joins inventory, selection, route, supervision, and report.
  Existing route tests remain green, and the full gate passes with 1,758 tests.
- [x] (2026-11-21 18:20Z) `EP-M5` `kani harnesses list` and
  `kani harnesses run` CLI contract. The CLI exposes route exclusivity,
  inventory inspection, budgets, evidence destinations, and verifier arguments;
  installs SIGINT/SIGTERM only for the duration of a run; returns 0 only for a
  complete pass, 1 otherwise, and `128 + signal` on interruption. Help
  snapshots and injected contract tests cover the surface. The full gate passes
  with 1,779 tests and 6 skips.
- [x] (2026-11-21 19:05Z) Posted the parser coordination comment on #250,
  naming `rust_prover_tools/kani_log/` as the landed parser layer, the open
  manifest and `decide()` work, and the captures 0.68.0 will need.
- [x] (2026-11-21 19:10Z) `EP-M6` Documentation, roadmap, schema
  document, and full gates. Added `docs/kani-supervised-execution.md` (the
  version-1 inventory, JSONL, and report schemas; configuration; discovery;
  outcome precedence; measurement limits; signal behaviour; adapter contract;
  test doubles; grammar qualification), updated the users', developers', and
  CLI design guides, README, and roadmap, and exported the typed public API from
  `rust_prover_tools`. `make all PYTEST_XDIST_WORKERS=4` passes with 1,779
  tests and 6 skips; the opt-in live 0.67.0 fixture also passes.
- [x] (2026-11-21 19:30Z) CodeRabbit review and remediation. Three local
  review passes were run after the deterministic gates; every actionable
  finding was fixed with a regression test or a cited, tested design record.
  The third pass added the bounded parser issue budget, distinct-destination
  validation, the prepare/execute split that keeps handlers off discovery and
  qualification, structured CLI option groups with no pylint suppressions,
  heartbeat recognized-progress precedence, and the alpha-only snapshot release
  note. The one residual limitation is the Cuprum 0.2.0b2 newline-free line
  accumulation, which cannot be bounded through the pinned public API and is
  documented as a limitation.
- [x] (2026-11-21 20:19Z) Self-review hardening after the third pass:
  inline `#[path]` directory semantics verified against `rustc`, path values
  restricted to `path` attributes, unregistered banners retained as typed
  errors, hard-link destination aliases rejected, and caller-supplied `--tests`
  forbidden. Each change has a focused regression test; the full deterministic
  gate is re-run before the draft is marked ready.
- [x] (2026-11-21 21:37Z) Fourth CodeRabbit pass: 17 actionable findings,
  all fixed with regression tests. The pass froze the snapshot proof
  environment with `EnvMode.REPLACE`, corrected child-module resolution for
  explicitly located files, added `cfg_attr` and hashed raw-string support, and
  tightened JSONL boundaries, schema typing, report publication, and
  destination encoding. CI also exposed that the fifteen parser corpus logs
  were excluded by the global `*.log` ignore; the fixture directory is now an
  explicit exception and the corpus is tracked. The full deterministic gate
  passes with 1,886 tests and 6 skips.
- [x] (2026-11-21 22:02Z) Fifth CodeRabbit pass: 23 findings, with the two
  test-style suggestions documented as skips and the corpus finding already
  landed at the reviewed head. The pass kept discovery and execution in one
  workspace, resolved qualified identities before aliases, bounded retained
  parser state, joined the child before a monitor failure, and kept report
  publication independent of harness- and run-finish sink failures. It also
  completed the Rust attribute grammar for whitespace, nested `cfg_attr`,
  string continuations, and Unicode escapes. The full deterministic gate passes
  with 1,898 tests and 6 skips.
- [x] (2026-11-21 22:49Z) Sixth CodeRabbit pass: 24 findings. The substantive
  findings were fixed with regression tests: recorded target selectors, serial
  `--jobs=1` output, snapshot routing rejection, a repository-root working
  directory, the remaining Rust attribute forms, an attempt-wide record budget,
  cover and other-result reconciliation, assertion-only expected panics,
  non-fatal independent diagnostics, bounded `MemorySink`, complete resource
  extrema, and monitor cleanup. Three findings are documented skips: the
  tested-example loader does not exist in this repository, Cuprum 0.2.0b2
  exposes no chunk-level line hook, and the test parameterization suggestions
  would churn existing test conventions without changing behaviour. The full
  deterministic gate passes with 1,911 tests and 6 skips.
- [x] (2026-11-21 23:30Z) Seventh CodeRabbit pass: 18 findings. Fixed the
  standard-output collision in every mode, NFC identifier normalization, the
  aligned original/masked attribute slices, nested `cfg_attr` delimiters and
  conditional paths, caller target selectors, duplicate result kinds, the
  solver-phase second banner, truncation-marker budgets, lexical symlink
  resolution, builder launch errors, and failed-sink offsets. The three
  documented skips remain the documentation loader, Cuprum's upstream
  incomplete-line buffer, and test-style-only refactors. The full deterministic
  gate passes with 1,922 tests and 6 skips.
- [x] (2026-11-22 00:15Z) Eighth CodeRabbit pass: 25 findings. Fixed the
  high-value findings: absolute `::kani` attributes, XID identifiers, parser
  refusal details, inventory-summary rejection, unknown result kinds, frozen
  environment snapshots, Boolean measurements, one-row table details, shared
  launch shielding, empty-group absence, byte-only meminfo fields, memory-sink
  budgets, expected-identity validation, late-signal status, and launcher
  protocol documentation. The residual skips are the absent documentation
  loader, non-portable standard-output alias detection, blocking filesystem
  writes on the event loop, cgroup mount resolution, observed-identity
  accounting, requested-stop evidence, and test-style-only refactors. The full
  deterministic gate passes with 1,929 tests and 6 skips.
- [x] (2026-11-22 00:35Z) Live fixture rerun after the selector changes:
  `RPT_LIVE_KANI=1` passes both tests in 1,400 seconds under the shared compile
  pool, with a 900-second per-harness budget. The fixture verifies a passing
  library proof, a passing `#[path]` proof, and a failing proof through the
  real Kani 0.67.0 driver.
- [x] (2026-11-22 00:39Z) Ninth CodeRabbit pass: 24 findings. Fixed escaped
  single quotes, broad Unicode harness headers, LF-only archive splitting,
  formatter-only quote removal, bounded issue text, empty-source refusal,
  cross-namespace selection ambiguity, non-finite JSON values, signal status
  when evidence publication fails, and the registry example. Residual skips:
  the absent documentation loader, XID-table differences beyond Python's
  identifier rules, shared-record byte sub-budgets, cgroup mount resolution,
  observed-identity accounting, requested-stop evidence, the injected snapshot
  probe environment, blocking diagnostic writes on the event loop, and
  test-style-only refactors. The full deterministic gate passes with 1,929
  tests and 6 skips; the live fixture also passes.

## Surprises & discoveries

- Observation: Kani 0.67.0 is installed on this host at
  `~/.kani/kani-0.67.0`, so the CLI's options and the corpus provenance could
  be verified directly rather than guessed. Evidence:
  `~/.kani/kani-0.67.0/bin/kani-driver kani --help` lists `--output-format` with
  `[default: regular]` and `[possible values: regular, terse, old]`, and
  `--exact` documents exact fully qualified matching. Impact: the registry can
  force `--output-format=regular` and forbid the spelling variants that would
  break the grammar.
- Observation: Cuprum's `SafeCmd.start()` forces capture on, because a handle
  must be able to report what the child said. `SafeCmd.run()` honours
  `capture=False`. Evidence: `cuprum/_process_handle/owned_start.py` calls
  `_with_capture`. Impact: supervision must use `run()` plus an `observe` hook
  for the PID, not `start()`.
- Observation: Combobulate's parser is whole-text, but its grammar is strictly
  line-delimited, and its `Reader` peeks and consumes one line at a time.
  Evidence: `scripts/docs_validation/kani_log/reader.py` and `extract.py` at
  commit `9dd6e3c`. Impact: an incremental state machine can reproduce it
  exactly; whole-text `parse()` becomes feed-line plus finish, which makes the
  equivalence test straightforward.
- Observation: the reader records refusals and raises at `finish` rather
  than raising on the first bad line. This keeps the selection record and the
  verdict of a proof that was stopped mid-report available to the supervisor
  while still failing a complete call closed. Evidence: `KaniLogReader.facts`
  reports `selection_records`, `verdicts`, and `truncation_reason` before
  `finish` raises. Impact: the classifier can attribute an interruption to the
  harness that was actually running.
- Observation: the corpus contains exactly fifteen logs: `counterexample`,
  `default_unwind`, `expected_panic`, `fail_fast`, `in_place`,
  `insufficient_unwind`, `list`, `multiplication_timeout`,
  `seeded_wrapping_mutant`, `selected_harness`, `truncated`,
  `unsatisfied_cover`, `unsupported`, `vacuous_assumption`, and `verified`.
  Evidence: `scripts/tests/fixtures/kani_logs/` at `9dd6e3c`. Impact: the port
  keeps all fifteen and their `expected.json` facts.
- Observation: self-review against `rustc` found two discovery semantics that
  the earlier tests did not pin. An inline `#[path = "..."]` module is itself
  the directory for its plain children, including in a non-mod-rs file, and a
  string-valued attribute such as `#[doc = "..."]` is not a `#[path]`. Evidence:
  `rustc --edition 2021` resolves
  `#[path = "thread_files"] mod thread { mod baz; }` only from
  `src/thread_files/baz.rs`, and a `#[doc]` module test failed before the path
  fact was restricted to a `path` attribute. Impact: discovery now computes the
  inline directory from the path root and reads path values only from `path`;
  regression tests cover both.

## Decision log

- D1. Decision: supervision uses `SafeCmd.run()` with
  `RunOutputOptions(capture=False, on_line=...)` and
  `ExecutionContext(process_group=OWN_GROUP)`, and obtains the child PID and
  `exec_id` from a scoped `cuprum.observe()` hook's `start` event. Rationale:
  `start()` forces capture, which would retain a whole proof log. `run()`
  honours `capture=False`, its cancellation path settles the owned process
  group, and the `start` event is documented to carry `pid`. Date/Author:
  2026-11-21, implementation agent.
- D2. Decision: implement the incremental parser as the single source of truth;
  `parse(text)` feeds lines and calls `finish(0)`. Rationale: a separate
  whole-text port could drift from the live reader. One state machine makes
  line-fed equivalence structural rather than hoped for. Date/Author:
  2026-11-21, implementation agent.
- D3. Decision: classify a single observed harness whose name differs from the
  requested name as `no-match`, with the observed name in the detail.
  Rationale: the documented outcome vocabulary has no separate "wrong-identity"
  status; `no-match` is the selection-mismatch family and is accurate: the
  requested identity did not match. Date/Author: 2026-11-21, implementation
  agent.
- D4. Decision: add `error` as a supervision status for launch failures and
  malformed verifier output, and document its mapping in the users' guide.
  Rationale: the required vocabulary distinguishes selection mismatch, proof
  failure, timeout, watchdog, interruption, and nonzero exit; launch and
  malformed-output failures are none of those and must not masquerade as a
  counterexample. Date/Author: 2026-11-21, implementation agent.
- D5. Decision: the inventory file is the authoritative strict interface, and
  source discovery produces the same model. `--inventory` loads and validates a
  version-1 document; without it, discovery runs. Rationale: the requirement
  asks for discovery and/or an explicit interface; providing both lets a
  consumer pin an inventory without copying discovery. Date/Author: 2026-11-21,
  implementation agent.
- D6. Decision: register `--output-format`, `--output-into-files`, `--quiet`
  and `-q` as forbidden output spellings, and `--harness`, `--exact`,
  `--package`, and `-p` as forbidden selector spellings for 0.67.0. Rationale:
  each would either move evidence out of the parsed stream, suppress it, or
  override the supervisor's exact selection. Proof-affecting flags such as
  `--unwind`, `--default-unwind`, and `-Z` remain allowed. Date/Author:
  2026-11-21, implementation agent.
- D7. Decision: heartbeat and sampling share one periodic monitor tick, but
  have independent intervals. Rationale: one task means one ordering of
  samples, heartbeats, deadline checks, and watchdog decisions; independent
  intervals keep the configuration honest. Date/Author: 2026-11-21,
  implementation agent.
- D8. Decision: the CLI's human sink defaults to standard output; standard
  error is reserved for adapter diagnostics and expected errors; the JSONL sink
  is off unless a path or stream is configured, and the report is returned and
  written only when a path is configured. Rationale: live human evidence
  belongs in the flushed job log, alongside the final table, while standard
  error stays a clean diagnostic channel; library consumers retain control of
  destinations. Date/Author: 2026-11-21, implementation agent.
- D9. Decision: the inventory scanner recognizes `#[kani::proof]` and
  `#[kani::proof_for_contract]`, and marks a harness `conditional` when any
  enclosing `cfg` attribute is anything other than `cfg(kani)`. Rationale:
  `cfg(kani)` is the normal Kani activation gate and is active under the
  verifier; every other `cfg` is an unevaluated condition that discovery must
  not pretend to resolve. Date/Author: 2026-11-21, implementation agent.
- D11. Decision: split qualification into `kani_qualification.py` and keep
  `kani_release.py` as the installation and command-construction boundary.
  Rationale: the repository's 400-line module limit forced a split, and the
  qualification sequence is the part a supervised run must perform once. The
  module-level `run_command` reference remains the shared patch point so
  existing route tests and callers are unaffected. Date/Author: 2026-11-21,
  implementation agent.
- D15. Decision: accept a Kani failure explanation without a source location,
  while still requiring one explanation per counted failure. Rationale: Kani
  0.67.0 may omit the optional trace location, but the verdict still reports
  the proof as failed; rejecting the log would turn a valid failure into a tool
  error. The count reconciliation still makes a deleted explanation detectable.
  Date/Author: 2026-11-21, implementation agent.
- D16. Decision: a successful Kani classification requires an explicit zero
  exit status; an absent status is an error. Rationale: only a supervisor stop
  or a launch failure can leave a child without a status, and both are
  classified earlier. Treating an impossible absence as success would weaken
  the exact-success rule. Date/Author: 2026-11-21, implementation agent.
- D13. Decision: a JSONL record that exceeds its byte budget is shortened by
  bounding its string fields before serialization, with a minimal valid
  `truncated` record as the last resort. Rationale: cutting a serialized line
  produces invalid JSON and makes the whole evidence stream unreadable through
  the strict reader. Keeping every record valid preserves the stream's value
  after a pathological line. Date/Author: 2026-11-21, implementation agent.
- D14. Decision: a test-target harness composes `--tests` because Kani 0.67.0
  only builds an integration-test target when that flag is present. Rationale:
  exact selection cannot match a target Kani never built. Date/Author:
  2026-11-21, implementation agent.
- D12. Decision: the supervision layer's adapter seam is an
  `observer(spec)` factory plus `classify(evidence, observer)`. Rationale:
  classification needs facts accumulated while output streams, but the generic
  runner must not know a prover's grammar. A per-attempt observer keeps the
  parser state private to the adapter and leaves the runner's timing,
  measurement, evidence, and accounting untouched. Date/Author: 2026-11-21,
  implementation agent.
- D10. Decision: no new dependency; JSON, hashing, parsing, and process
  inspection use the standard library. Rationale: the capability needs no
  third-party runtime package, and the repository's gates already cover the
  standard library. Date/Author: 2026-11-21, implementation agent.
- D17. Decision: `DiagnosticSinks` and the CLI reject two destinations that
  resolve to one file. Rationale: human and JSONL records have different
  grammars, so sharing an append handle produces a file that is neither, and
  JSONL offsets silently stop matching; the report's atomic write would destroy
  evidence at the same path. Paths are resolved first so symlink aliases cannot
  evade the check. Date/Author: 2026-11-21, implementation agent.
- D18. Decision: discovery, selection, and route qualification are a separate
  `prepare_harnesses` phase from `execute_harnesses`. Rationale: the CLI's
  handlers can stop a supervised child but cannot stop Cargo metadata or a
  probe, which run synchronously; installing handlers only around execution
  leaves SIGINT and SIGTERM at their prompt default dispositions during
  preparation instead of recording a signal that cannot take effect. The
  combined `run_harnesses` remains the one-call API. Date/Author: 2026-11-21,
  implementation agent.
- D19. Decision: the reader retains at most `MAX_RETAINED_ISSUES` refusals
  plus one exhaustion sentinel. Rationale: `finish` raises the first retained
  issue, so a bounded prefix preserves every refusal decision while a noisy
  harness cannot size the attempt's memory; the sentinel keeps exhaustion
  distinguishable from a quiet report. Date/Author: 2026-11-21, implementation
  agent.

## Outcomes & retrospective

`EP-M1` is complete: `rust_prover_tools/kani_log/` provides the ported grammar,
value model, incremental reader, report reconciliation, exact-version registry,
and the fifteen-log corpus with provenance. `CommandTimedOutError` preserves
the existing message and chains the Cuprum timeout. The full repository gate
passes with 1,580 tests and 5 skips.

`EP-M5` is complete: `kani_harness_cli.py` registers `kani harnesses list` and
`kani harnesses run`, disables `INPUT_*` binding for the subtree, installs
SIGINT and SIGTERM only for one run, and maps a typed report to the documented
exit status. Its help snapshots and injected contract tests cover route
exclusivity, selector conflicts, drift, interruption, and a closed standard
output.

`EP-M4` is complete: `kani_qualification.py` exposes the one-time legacy
qualification and `prepare_snapshot_proof` the one-time snapshot preparation;
`supervision/kani.py` classifies attempts under the documented precedence; and
`kani_harness_run.py` composes inventory, exact selection, route, supervision,
and report behind `run_harnesses`. The legacy `exec_kani` and snapshot
`run_snapshot` behaviour is unchanged, as their existing suites show.

`EP-M3` is complete: `rust_prover_tools/kani_inventory/` provides the typed
inventory models, the strict version-1 document, a comment-and-literal-aware
Rust scanner that follows inline modules and `#[path]`, fail-closed Cargo
package and target selection, exact harness selection, and argv composition
with a conflict matrix for selector and output spellings.

`EP-M2` is complete: `rust_prover_tools/supervision/` provides the typed
models, validated policy, injectable protocols, Linux procfs and cgroup v2
readers, flushed human and JSONL sinks, strict version-1 schema readers,
multiset accounting, the async Cuprum launcher, the monitor-driven attempt
supervisor, and the run orchestrator. The production defaults remain the source
plan's 1,200 seconds, 5 seconds, and 1,024 MiB.

The final change exceeds the initial file and code budgets. The issue's five
tasks include the full fifteen-log parser corpus, two route integrations, a new
CLI surface, and the schema reference; the user's instruction authorized that
full scope. The overage is recorded here rather than treated as a silent
tolerance breach.

All six milestones are implemented. The acceptance evidence is:

- a second consumer can supply different packages, paths, inventories,
  budgets, evidence destinations, and a route without copying the runner,
  parser, watchdog, or telemetry code;
- deterministic doubles cover exact selection and preserved execution
  configuration, no and multiple matches, missing, duplicate, and unexpected
  executions, proof failure, nonzero exit, timeout and watchdog cleanup,
  continuation, silent and verbose children, unavailable cgroup data, and
  partial reporting after interruption;
- the version-qualified corpus rejects incomplete and ambiguous success
  evidence;
- examples in the users' guide and README show live human output, JSONL,
  inspectable summaries, and programmatic result access;
- documentation explains configuration, outcome semantics, measurement
  limits, signal behaviour, and adoption.

The retrospective will be completed after CodeRabbit review and PR delivery.

## Context and orientation

The inspected base revision is `085de72` on `main`. Key existing modules:

- `rust_prover_tools/commands.py` is the only module that runs external
  programmes. `CommandSpec` describes an invocation, `run_command` executes it
  through Cuprum and translates `TimeoutExpired` into a plain
  `ProverToolError`, and `TailCapture` keeps a bounded tail through an
  `on_line` hook.
- `rust_prover_tools/kani_release.py` owns the qualified legacy route:
  `prepare_kani_command` validates the installation, arguments, Cargo routing,
  and environment; `exec_kani` probes the compiler and verifier identities and
  then runs the proof with `echo=True`.
- `rust_prover_tools/kani_snapshot/run.py` owns the snapshot route:
  `run_snapshot` revalidates the active generation, builds the proof
  environment, and runs the launcher with a bounded tail.
  `rust_prover_tools/kani_snapshot/verification_env.py` builds the child
  environment; `rust_prover_tools/kani_snapshot/identity.py` validates
  forwarded arguments.
- `rust_prover_tools/observability.py` owns opt-in structured events and
  correlated spans. `operation_span` closes a span with a bounded outcome and
  only the exception class name on failure.
- `rust_prover_tools/cli.py` builds the Cyclopts application; command modules
  such as `kani_exec_cli.py` and `kani_snapshot_cli.py` are thin adapters.
- `rust_prover_tools/kani_snapshot/producer/compiler_floor.py` is pinned
  upstream evidence and must not change.
- The test framework is pytest with `pytest-xdist`, `pytest-bdd`, `cmd-mox`,
  `hypothesis`, and `syrupy`. The gate sequence is `make fmt`, `make check-fmt`,
  `make lint`, `make typecheck`, `make test`, `make spelling`,
  `make markdownlint`, `make nixie`, and `make test-workflow-contracts`.
  `make all` runs most of them.
- The repository is pre-1.0 (`version = "0.1.0"`). New public APIs are
  additive; no deployed consumer requires compatibility shims.

Budget survey: the repository already has about 120 Python modules, many of
them small because of the 400-line pylint limit. There is no completed plan in
`docs/execplans/` with a comparable size, so this plan's budgets are derived
from the ported source (about 1,300 lines of parser and corpus), the Cuprum
supervision surface (about 1,500 lines of implementation and tests), the
inventory scanner (about 1,000), the route and CLI integration (about 1,200),
and documentation (about 1,000). The code budget of 9,000 net lines includes
tests and leaves contingency of roughly 25 per cent.

## Conformance basis

The upstream artefacts inspected are:

- `leynos/cuprum` issue #723, "extended (kani) job killed with exit 143 since
  26 October", and its implementation-plan comment `6995055883`. These supply
  the source-plan requirements: per-harness invocation, inventory rejection
  rules, start records, streamed timestamps, heartbeats, process-group RSS and
  host memory sampling, cgroup observations, the 1,200-second / 5-second /
  1,024-MiB defaults, the memory watchdog, outcome vocabulary, multiset
  accounting, continuation after failure, signal handling, and summary output.
- `leynos/rust-prover-tools` issue #260, which adapts that plan into reusable
  library and CLI capability with typed Python objects, a versioned schema, and
  a documented adapter contract.
- `leynos/rust-prover-tools` issue #250, which owns the shared Kani parser
  vocabulary and result analysis. This plan lands the parser layer in
  `rust_prover_tools/kani_log/` and leaves the manifest and `decide()` layer
  open.
- Combobulate commit `9dd6e3c24306ee947ca3bf3bc76339cd67509f1d`, file
  `LICENSE` (ISC, Copyright © 2026 Payton McIntosh), and
  `scripts/docs_validation/kani_log/`.

Named requirements derived from those artefacts:

- `REQ-INVENTORY`: package-qualified, inspectable inventory with exact
  selection and multiset accounting.
- `REQ-EVIDENCE`: live, flush-on-write human and JSONL evidence that survives
  interruption and remains bounded in memory.
- `REQ-MEASURE`: per-harness process-group RSS and host memory samples with
  explicit scope and explicit unavailability.
- `REQ-LIMITS`: per-harness timeout and host-memory watchdog with attributable
  termination.
- `REQ-OUTCOMES`: typed outcomes, preserved raw termination evidence, and
  documented precedence.
- `REQ-API`: typed Python API and CLI over one implementation.
- `REQ-DOCS`: documented configuration, semantics, limitations, and adoption.

Mapping:

```plaintext
REQ-INVENTORY -> EP-M3 -> tests/test_kani_inventory_*.py
REQ-EVIDENCE  -> EP-M2 -> tests/test_supervision_evidence.py
REQ-MEASURE   -> EP-M2 -> tests/test_supervision_resources.py
REQ-LIMITS    -> EP-M2 -> tests/test_supervision_runner.py
REQ-OUTCOMES  -> EP-M4 -> tests/test_kani_harness_runner.py
REQ-API       -> EP-M4, EP-M5 -> tests/test_kani_harness_cli.py
REQ-DOCS      -> EP-M6 -> docs/kani-supervised-execution.md
```

## Verification plan

### Abstract model

A supervised run takes a configuration `C`, an expected inventory `E` (a
multiset of package-qualified identities), and an environment providing a
clock, a sleeper, a launcher, a resource probe, evidence sinks, and an
interrupt source. It produces a report `R` with one result per executed
identity, one `not-run` result per unexecuted identity, an accounting
comparison between `E` and the executed multiset, and an overall outcome.

For each identity the supervisor selects a single stop reason from an ordered
set: the first of timeout, memory watchdog, interruption, or ordinary child
exit that occurs. It classifies the identity from the stop reason, the launch
result, the parser's selection records, the parser's verdict, and the raw exit
status, in the documented precedence order.

### Invariants and lemmas

- `INV-BOUNDED`: parsing and evidence retain at most a configured number of
  bytes per line and per stream tail, and bounded prefixes of retained record
  collections (issues, blocks, checks, and failures). The one exception is
  Cuprum 0.2.0b2's line-drain buffer, which can retain an unterminated line
  before the bounded hook receives it; no component retains a whole proof log.
- `INV-EXACT`: a harness is `passed` only when exactly one selection record
  matches the requested pretty name exactly, the verdict is successful, the
  parser reconciled the whole log, and the child exited zero.
- `INV-ACCOUNT`: every expected identity appears exactly once in the aggregate;
  a missing, duplicate, or unexpected execution makes the run incomplete.
- `INV-FIRST-STOP`: the recorded stop reason is the first stop event observed,
  and later stop attempts do not replace it.
- `INV-CONTINUE`: a failed, timed-out, or watchdog-stopped harness does not
  prevent later harnesses from running, unless the run is interrupted.
- `INV-FINALIZE`: ordinary completion, failure, and catchable interruption
  finalize the available report; an abrupt kill may lose only what was not yet
  flushed.
- `L-PARSE-EQUIV`: feeding a complete log line by line and calling
  `finish(exit_code)` yields the same facts as `parse(text)`.
- `L-PRECEDENCE`: classification applies the stop, launch, selection,
  malformed-output, verdict, exit-status, passed order, so no weaker evidence
  can override a stronger one.
- `L-MEASURE`: every reported summary is a function of the samples or cgroup
  observations actually recorded; an unavailable value is never rendered as
  zero.

### Axioms (trusted, not verified here)

- `AXIOM-CUPRUM-OWN-GROUP`: Cuprum's `OWN_GROUP` spawns the child as a new
  session and process-group leader on Linux, so the child PID is the group ID,
  and cancellation settles the whole group. Source: Cuprum 0.2.0b2
  `ProcessGroupPolicy` documentation.
- `AXIOM-CUPRUM-START-PID`: the `start` observe event carries the child PID.
  Source: `cuprum/_subprocess_spawn.py`, `cuprum/_pipeline_spawn.py`.
- `AXIOM-PROC-STAT`: `/proc/<pid>/stat` field 5 is the process group and field
  24 is the resident set size in pages, as documented in `proc(5)`.
- `AXIOM-MEMINFO`: `/proc/meminfo` reports `MemTotal` and `MemAvailable` in
  kibibytes.
- `AXIOM-KANI-067`: Kani 0.67.0 prints the regular report grammar captured in
  the corpus when `--output-format=regular` is selected, and `--harness` with
  `--exact` matches the fully qualified pretty name.
- `AXIOM-CGROUP-V2`: cgroup v2 `memory.current`, `memory.peak`, and
  `memory.events` carry the documented values when present, and `memory.peak`
  may be absent on older kernels.

### Methods per obligation

- Obligation: `INV-BOUNDED` and `L-PARSE-EQUIV`.
  Method: parameterized fixture tests over all fifteen corpus logs, plus a
  Hypothesis property that feeds each log in random line chunks and injects
  arbitrary chatter between reports. Rationale: the corpus is the normative
  grammar; generated chunking explores the incremental boundary without a live
  prover. Artefact: `rust_prover_tools/unittests/test_kani_log_reader.py`,
  `tests/fixtures/kani_logs/`. Evidence: `make test`; the property must fail if
  `feed_line` buffers the whole log or if `finish` skips reconciliation.
  Non-vacuity: the corpus reaches every successful, refused, timed-out,
  unsupported, cover, panic, and multi-harness class; a mutation that deletes a
  reconciliation is rejected by at least one corpus log.
- Obligation: `INV-EXACT`, `INV-FIRST-STOP`, `INV-CONTINUE`, `L-PRECEDENCE`.
  Method: deterministic tests with a virtual clock, scripted launcher, and fake
  resource probe. Rationale: every branch depends on timing and child
  behaviour; doubles make each branch exact and fast without real sleeps.
  Artefact: `rust_prover_tools/unittests/test_supervision_runner.py`,
  `tests/test_kani_harness_runner.py`. Evidence: `make test`; the no-match,
  multiple-match, failed, nonzero, timeout, watchdog, launch-error, and
  interruption cases each assert their status and the first stop reason.
  Non-vacuity: a scripted child produces both silent and verbose output; the
  timeout case proves the launcher's stop was requested; the continuation case
  proves a later harness still runs.
- Obligation: `INV-ACCOUNT`.
  Method: parameterized multiset tests plus CLI drift tests. Rationale:
  accounting is a finite algebra; explicit cases cover every combination of
  missing, duplicate, and unexpected. Artefact:
  `rust_prover_tools/unittests/test_supervision_accounting.py`. Evidence:
  `make test`; a duplicate expected identity is rejected at inventory
  construction, and a duplicate execution is rejected at aggregation.
  Non-vacuity: each case asserts a non-empty difference set.
- Obligation: `INV-FINALIZE`.
  Method: an in-memory sink test that interrupts mid-run and asserts the
  partial JSONL and the final table; plus one real-process witness. Rationale:
  finalization is an ordering property best checked with a deterministic
  interrupt, and the real process proves the cancellation path. Artefact:
  `rust_prover_tools/unittests/test_supervision_runner.py`,
  `tests/test_supervision_process_witness.py`. Evidence: `make test`; the JSONL
  reader sees a run-start, a harness-start, and a harness-finish with
  `interrupted` before the run-finish. Non-vacuity: the witness child prints
  before it is stopped, so a silently dropped stream would be visible.
- Obligation: `L-MEASURE`.
  Method: fake procfs/cgroupfs tests with available, missing, malformed, and
  permission-denied files. Rationale: filesystem failure modes are finite and
  best enumerated. Artefact:
  `rust_prover_tools/unittests/test_supervision_resources.py`. Evidence:
  `make test`; each unavailable value carries its reason and the proof still
  runs. Non-vacuity: at least one case has a real integer value so absence is
  not the only branch exercised.
- Obligation: `REQ-DOCS`.
  Method: Markdown lint, spelling, Mermaid validation, and link review.
  Rationale: documentation correctness is a repository gate. Artefact:
  `docs/kani-supervised-execution.md`, `docs/users-guide.md`,
  `docs/developers-guide.md`, `docs/prover-tools-cli-design.md`, `README.md`,
  `docs/roadmap.md`. Evidence: `make markdownlint`, `make spelling`,
  `make nixie`. Non-vacuity: the docs state the defaults and limitations
  explicitly and are cross-checked against the code constants.

### Formal verification decision

No bounded model check or formal proof is planned. The obligations are
orchestration and file-format properties over a small, explicit state space;
deterministic scripted doubles and parameterized tests discharge them more
precisely than a formal model would justify. The residual gap is the real
Cuprum and kernel behaviour, which the single real-process witness and the
opt-in live Kani fixture test exercise at the boundary.

### Suggested BDD features

The repository already runs `pytest-bdd`. A proposed feature file
`tests/features/kani_harness_supervision.feature` would record:

```gherkin
Feature: Supervised per-harness Kani execution
  Scenario: A passing harness is counted exactly once
    Given an inventory containing one harness
    When the harness prints a successful regular report and exits zero
    Then the aggregate result is passing and complete

  Scenario: A memory watchdog stop is attributable
    Given a harness whose host available memory falls below the threshold
    When the supervisor samples the host
    Then the harness is stopped and classified as memory-exhausted
    And the remaining harnesses still run

  Scenario: An interrupted run preserves its evidence
    Given a harness that streams output
    When the run is interrupted
    Then the partial JSONL names the interrupted harness
    And every unexecuted harness is reported as not-run
```

The CLI contract tests exercise these scenarios without a live prover.

## Plan of work

The work follows the five tasks in the issue's coding plan, with a final
documentation milestone.

### `EP-M1` Shared Kani 0.67.0 grammar and incremental reader

Create `rust_prover_tools/kani_log/` with `syntax.py`, `model.py`, `reader.py`,
`extract.py`, `registry.py`, `__init__.py`, and `ATTRIBUTION.md`. Port the
Combobulate grammar, model, and reader logic with ISC attribution. The reader
is an incremental state machine exposing `feed_line(stream, text)`,
`finish(exit_code)`, and a `facts` snapshot containing selection records,
verdicts, contradictions, summary errors, and truncation.
`parse(text, exit_code=0)` feeds the lines and calls `finish`. `registry.py`
registers exactly 0.67.0 with its grammar, output arguments
(`--output-format=regular`), forbidden selector and output spellings, and the
pretty-name derivation rule (the module path relative to the crate root plus
the function name).

Copy the fifteen-log corpus from Combobulate commit `9dd6e3c` into
`tests/fixtures/kani_logs/`, with `expected.json` and an adapted
`provenance.json` recording the source commit, licence, and stream order.

Add `CommandTimedOutError(ProverToolError)` in `errors.py`, carrying
`timeout_seconds` and `argv`, and raise it from `run_command` with the existing
message and a chained `TimeoutExpired` cause. Extend the command timeout tests.

Red evidence: the new parser tests fail because the package does not exist.

### `EP-M2` Prover-neutral supervision, measurement, evidence, and reporting

Create `rust_prover_tools/supervision/` with:

- `models.py`: frozen slotted enums and value types, including
  `ExecutionStatus`, `ProofVerdict`, `StopReason`, `Measurement`,
  `ResourceSample`, `CgroupObservation`, `HarnessSpec`, `HarnessResult`,
  `RunReport`, and `InventoryDrift`.
- `config.py`: validated `SupervisionConfig` with the source-plan defaults
  (1,200-second timeout, 5-second sample interval, 1,024-MiB watchdog
  threshold), heartbeat interval, bounded line size, and diagnostic
  destinations.
- `protocols.py`: injectable `Clock`, `Sleeper`, `Launcher`, `ResourceProbe`,
  `InterruptSource`, and `EvidenceSink` protocols.
- `resources.py`: Linux procfs RSS and meminfo readers, cgroup v2 discovery
  and memory readers, all returning `Measurement` values; reject non-Linux
  hosts in the runner.
- `sinks.py`: flushed JSONL version-1 records, human-readable records,
  bounded line handling, byte-offset diagnostics, and in-memory doubles.
- `report.py`: deterministic table rendering, final JSON summary, strict
  report reader, and the version-1 schema helpers.
- `accounting.py`: multiset comparison reused by inventory and the runner.
- `launcher.py`: the async Cuprum launcher using `run()` with
  `capture=False`, `OWN_GROUP`, a scoped `observe` hook, and cancellation.
- `runner.py`: sequential orchestration with one monitor task, heartbeats,
  sampling, deadline and watchdog checks, first-stop accounting, continuation,
  interruption, and finalization.

Tests use a virtual clock, scripted launcher, in-memory sinks, and fake
procfs/cgroupfs. One real-process witness proves group cancellation and
heartbeat emission.

Red evidence: supervision tests fail because the package does not exist.

### `EP-M3` Package-qualified Kani inventory and exact selection

Create `rust_prover_tools/kani_inventory/` with:

- `models.py`: frozen `HarnessSpec`, `PackageInventory`, `HarnessInventory`,
  `HarnessIdentity`, strict version-1 JSON load and dump, and duplicate and
  empty-package rejection.
- `rustsource.py`: a lexical scanner that skips comments, string literals, raw
  strings, char literals, and lifetimes, tracks inline modules and brace depth,
  follows Rust 2018 module files and `#[path]`, and records proof attributes and
  `cfg` marks.
- `discover.py`: `cargo metadata --no-deps --format-version 1` through
  `CommandSpec`/`run_command`, package selection, lib/bin/test roots, and the
  recursive scan with cycle and ambiguity rejection.
- `select.py`: exact selection of requested harnesses, rejection of ambiguous
  bare names, and argv composition (`--package`, `--harness`, `--exact`,
  registry output arguments, caller arguments), including conflict detection in
  separate and `=` forms on both sides of `--`.

Add a two-package fixture workspace under
`tests/fixtures/kani_harness_workspace/` covering `#[path]`, inline modules, a
conditional proof, a duplicate pretty name, and an empty package.

Red evidence: inventory tests fail because the package does not exist.

### `EP-M4` Qualify-once route seams, Kani classification, and public API

Refactor `kani_release.py` to extract
`qualify_kani_command(options, environment)` returning a qualified base (driver
argv prefix, cwd, environment, installation root) after validation and the
three identity probes, and make `exec_kani` delegate with unchanged stage order
and behaviour. Refactor `kani_snapshot/run.py` to extract
`prepare_snapshot_proof(options, hooks)` returning the validated runtime,
working directory, environment, and launcher prefix; make `run_snapshot`
delegate with unchanged behaviour. Add a `qualified_snapshot_proof` path that
accepts one resolution and derives every harness invocation from it.

Create `rust_prover_tools/supervision/kani.py` with the Kani classifier and
`rust_prover_tools/kani_harness_run.py` with `run_kani_harnesses`, which:

1. loads or discovers the inventory,
2. selects the requested identities exactly,
3. qualifies the chosen route once,
4. composes and validates one argv per identity,
5. runs the supervision layer with the Kani launcher and parser,
6. classifies each result in the documented precedence,
7. returns the typed report.

Export the public surface through `rust_prover_tools/__init__.py` and the
package `__init__.py` files with explicit `__all__` and NumPy-style docstrings.

Red evidence: the qualify-once tests fail and the route tests prove the original
`exec_kani`/`run_snapshot` behaviour is unchanged.

### `EP-M5` CLI contract

Create `rust_prover_tools/kani_harness_cli.py` with a `harnesses`
sub-application registered under `kani`, containing `list` and `run`. Disable
generic `INPUT_*` binding for this subtree. `list` prints the resolved
inventory JSON. `run` writes live human output and the final table to standard
output, diagnostics to standard error, installs SIGINT and SIGTERM handlers
only in this module, finalizes interrupted runs, and returns `128 + signal` on
interruption, `0` only for a complete passing run, and `1` for other completed
runs or expected errors.

Update the help snapshots and add injected CLI contract tests for route
exclusivity, selector conflicts, drift, failure, interruption with summary, and
a closed standard output.

Add an opt-in live 0.67.0 fixture test with a passing harness, a `#[path]`
module, and a failing harness, skipped unless the environment enables it.

### `EP-M6` Documentation and delivery closure

Create `docs/kani-supervised-execution.md` covering configuration, discovery
limits, status precedence, watchdog and measurement limitations, signal and
exit behaviour, the Cuprum long-line limit, examples, adapter contracts, test
doubles, grammar qualification, the qualify-once rationale, and the version-1
JSONL and report schemas. Update `docs/users-guide.md`,
`docs/developers-guide.md`, `docs/prover-tools-cli-design.md`, `README.md`, and
`docs/roadmap.md`. Run every gate and reconcile this plan.

## Milestones and plateaus

- `EP-M1`: the parser package, registry, corpus, and typed timeout error exist
  and pass their focused tests. Acceptance evidence: the corpus fixture tests,
  mutation tests, and the line-fed property pass. Conformance: only 0.67.0 is
  registered; no sibling producer file changed; the timeout error preserves the
  existing message. Recovery: revert the parser package and error change
  together. Remaining: supervision consumes the parser later. Compatibility:
  none; this is a pre-1.0 additive API.
- `EP-M2`: supervision, measurement, evidence, reporting, and the runner exist
  and pass deterministic tests plus the real-process witness. Acceptance
  evidence: runner tests for every status, stop reason, continuation, and
  interruption, and resource tests for every unavailable branch. Conformance:
  no library signal handler; no new dependency; bounded retention subject to
  the documented Cuprum drain exception. Recovery: revert the supervision
  package. Remaining: Kani classification and route wiring. Compatibility: none.
- `EP-M3`: inventory discovery, strict JSON, selection, and argv composition
  exist and pass their tests. Acceptance evidence: the fixture workspace tests
  and the conflict matrix. Conformance: no hard-coded package or harness name.
  Recovery: revert the inventory package and fixture. Remaining: route
  integration. Compatibility: the version-1 inventory format is new.
- `EP-M4`: the two routes expose qualify-once seams, the Kani classifier and
  `run_kani_harnesses` exist, and the public API is exported. Acceptance
  evidence: unchanged route tests, one-probe/one-generation tests, and the
  classification precedence matrix. Conformance: byte-identical legacy
  behaviour; no competing parser. Recovery: revert the seam extraction and new
  modules together. Remaining: CLI. Compatibility: none.
- `EP-M5`: the CLI exposes `harnesses list` and `harnesses run` with documented
  exit semantics. Acceptance evidence: injected CLI tests and the opt-in live
  fixture. Conformance: signal handlers only here; no `INPUT_*` binding.
  Recovery: revert the CLI module and snapshot changes. Remaining: docs.
  Compatibility: none.
- `EP-M6`: documentation and roadmap are current and all gates pass.
  Acceptance evidence: the gate logs. Conformance: every traced requirement has
  evidence; the upstream parser coordination comment is posted. Recovery: none
  needed. Remaining: PR delivery.

## Concrete steps

Working directory: the worktree root.

Red and green commands are run with `set -o pipefail` and `tee`:

```plaintext
Red:   env -u FORCE_COLOR make test PYTEST_ADDOPTS='-k kani_log' 2>&1 | tee /tmp/test-kani-log-<branch>.out
Green: the same focused command passes after the implementation.
Refactor: make fmt, make check-fmt, make lint, make typecheck run in order.
Full gates: make all, then make test-workflow-contracts.
```

Because the repository's gates are sequential and shared, the final full gate
run is delegated to a `scrutineer` subagent, which captures each log under
`/tmp` and returns a bounded report. No sub-agent runs repository gates.

## Validation and acceptance

A consumer can run:

```bash
prover-tools kani harnesses list --repo-root . --package example
prover-tools kani harnesses run --repo-root . --package example \
  --installation "$HOME/.kani/kani-0.67.0" --cwd . \
  --jsonl /tmp/example.jsonl --report /tmp/example-report.json
```

Observed behaviour:

- `list` prints deterministic JSON with one entry per discovered harness, its
  package, target, source location, pretty name, and conditional mark.
- `run` prints a flushed start line, timestamped child lines, heartbeat lines,
  and sample lines; writes JSONL incrementally; and ends with a table such as
  `example  verified  passed  2.1s  812 MiB  9,431 MiB  0`.
- A harness that exceeds 1,200 seconds, or whose host available memory falls
  below 1,024 MiB, is stopped and reported as `timed-out` or
  `memory-exhausted`, and later harnesses still run.
- An interrupted run returns `128 + signal`, reports the active harness as
  `interrupted`, and reports the rest as `not-run`.
- A missing, duplicate, or unexpected execution makes the run exit nonzero even
  when every executed child exited zero.
- The final report validates against the version-1 schema and includes each
  harness's identity, status, duration, resource summary, termination evidence,
  and diagnostic reference.

Quality criteria:

- Tests: `make test` passes, including the new tests and the corpus.
- Verification: every invariant and lemma above has an artefact and passing
  evidence; no obligation is left undischarged.
- Lint and typecheck: `make lint`, `make typecheck`, `make check-fmt`,
  `make spelling`, `make markdownlint`, `make nixie`, and
  `make test-workflow-contracts` pass.
- Performance: the supervision loop does no work proportional to log volume
  beyond one bounded line and one sink write per line.
- Security: no new network access, no shell invocation, no credential handling,
  and no unbounded metric label.

## Idempotence and recovery

All discovery, parsing, and reporting steps are read-only or write to explicit
paths. A run writes its JSONL append-only and its report atomically (write a
temporary file beside the destination and rename it). Re-running the same
command overwrites the report and appends to the JSONL, so earlier records
remain and diagnostic offsets continue from the existing file size. Reverting a
milestone means reverting its files together; no persistent state or migration
is involved.

## Artefacts and notes

Reconnaissance evidence:

- Combobulate `kani_log` modules and corpus are at commit `9dd6e3c`; the
  fifteen corpus logs and `expected.json` were inspected.
- Kani 0.67.0 help confirms `--output-format` default `regular`, possible
  values `regular`, `terse`, and `old`, and `--exact` fully qualified matching.
- Cuprum 0.2.0b2 exposes `SafeCmd.run`, `SafeCmd.start`, `RunningCommand`,
  `ProcessGroupPolicy.OWN_GROUP`, `RunOutputOptions.capture`, and the `start`
  observe event with `pid`.

## Interfaces and dependencies

No new dependencies. Authorized interfaces (names are prescriptive):

```python
# rust_prover_tools/kani_log/reader.py
class KaniLogReader:
    def feed_line(self, stream: LineStreamName, text: str) -> None: ...
    def finish(self, exit_code: int) -> KaniLog: ...
    @property
    def facts(self) -> KaniLogFacts: ...

# rust_prover_tools/kani_log/extract.py
def parse(text: str, *, exit_code: int = 0) -> KaniLog: ...

# rust_prover_tools/supervision/runner.py
async def run_supervised(
    specs: Sequence[HarnessSpec],
    config: SupervisionConfig,
    environment: SupervisorEnvironment,
) -> RunReport: ...

# rust_prover_tools/kani_harness_run.py
def run_kani_harnesses(options: KaniHarnessRunOptions) -> KaniHarnessRun: ...
```

Reuse policy: `commands.py` remains the only synchronous command boundary; the
async launcher builds the same `CommandSpec`-equivalent argv and environment
but runs through `SafeCmd.run()` because it must observe and cancel a live
process group. The Kani adapter owns classification and argv composition; the
supervision package owns timing, measurement, evidence, and accounting. No
caller may re-parse Kani logs or re-implement the watchdog.

## Revision note

- 2026-11-21: Initial draft after reconnaissance. The plan is self-contained and
  reflects the issue's coding plan, the Cuprum #723 source plan, the
  Combobulate parser at `9dd6e3c`, and the installed Kani 0.67.0 help output.
