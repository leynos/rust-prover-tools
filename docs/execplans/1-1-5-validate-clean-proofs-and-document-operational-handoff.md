# Validate clean proofs and document operational handoff

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS

Draft pull request: pending.

Branch: `1-1-5-validate-clean-proofs-and-document-operational-handoff`

Roadmap item: `1.1.5` in `docs/roadmap.md`. Parent milestone EP-M5 in
`docs/execplans/kani-monthly-snapshot-distribution.md`.

## Purpose / big picture

Roadmap items 1.1.1 to 1.1.4 delivered a pinned upstream recipe, validated pins
and authenticated manifests, an isolated consumer installer, and a guarded
build and publication workflow. Nothing has yet demonstrated that the consumer
commands accept a real hosted build, run the named proofs against the effective
compiler, reuse a warm cache, or refuse altered bytes and identities before
execution. No operations runbook tells an operator how to bootstrap the first
publication or recover a missed month.

After this work an operator can dispatch one non-publishing hosted build,
download its assembled release, and run `make kani-snapshot-verify` against it.
That target installs the release through the public command path in a fresh
environment, checks it, runs named positive harnesses, a deliberately false
harness and a compiler-floor harness, repeats the install from a warm cache,
and shows a corrupted payload and a changed identity being refused before
anything executes. The same work adds `docs/kani-snapshot-operations.md`, the
exact bootstrap, recovery, rollback and monitoring handoff, and synchronizes
the user, developer, CLI and design guides.

Terms used in this plan:

- *snapshot*: one immutable release identified by a tag of the form
  `kani-snapshot-YYYY-MM-<12 hex>-rN`.
- *pin*: the reviewed `tools/kani/snapshot.toml` document that names one
  snapshot's tag, upstream commit, builder commit, target and manifest digest.
- *harness*: one `#[kani::proof]` function whose named verdict appears in a
  Kani transcript.
- *transcript*: the combined standard output and error of one `kani` run.
- *setup*: the packaged launcher's extraction and toolchain installation,
  which must consume prebuilt bytes and compile no tool source.
- *consumer path*: the public commands `prover-tools kani snapshot install`,
  `prover-tools kani snapshot check`, and
  `prover-tools kani run --snapshot-pin`.
- *non-publishing run*: a `workflow_dispatch` of
  `.github/workflows/kani-monthly.yml` with `publish=false`, which still builds
  and attests but creates no release.

Nothing is published by this work, no repository setting changes, and the
scheduled trigger stays disabled.

## Constraints

- Do not publish, move a tag, create a release, or change a repository
  setting. `KANI_SNAPSHOTS_ENABLED` stays unset.
- Keep the legacy release path, `tools/kani/VERSION`, and every existing CLI
  option, output and environment behaviour unchanged.
- Keep the package Python-only and its declared minimum at Python 3.14.
- Do not commit an upstream checkout, generated binary, or build output.
- Ordinary `make test` performs no real Kani build, no network build and no
  hosted dispatch. The real smoke is opt-in.
- New producer modules stay pure: no `os`, `subprocess`, `socket`, `http`,
  `urllib`, `tempfile`, `shutil` or file-reading imports, as enforced by
  `tests/test_kani_snapshot_producer_purity.py`.
- Every module, class and function under `rust_prover_tools` and `tests` needs
  a NumPy-style docstring (`make interrogate`, 100%).
- Pylint limits apply: 400 module lines, 4 arguments, 10 locals, 70
  statements, complexity 8.
- Every external command in production code passes through
  `rust_prover_tools.commands` and Cuprum. The integration test is an adapter
  boundary and may invoke the public console script directly.
- A hosted non-publishing validation requires the builder already merged to
  `main`, with scheduling disabled. EP-M4 is merged at `6be9e71`; this branch
  does not change the builder path. If the dispatch is unavailable, record it
  as outstanding and leave EP-M5 and roadmap 1.1.5 open.
- Follow the documentation style guide, the spelling policy, and the Markdown
  gates (`make fmt`, `make markdownlint`, `make nixie`).

## Tolerances (exception triggers)

- Files: more than 45 non-ExecPlan files changed: stop and escalate.
- Code: more than 1,800 net lines of Python and Rust fixture code: stop and
  escalate.
- Configuration: more than 200 net lines across `Makefile`, `pyproject.toml`
  and `.github/workflows/`: stop and escalate.
- Documentation: more than 1,600 net lines excluding ExecPlan files: stop and
  escalate.
- Interface: any change to a public CLI option or snapshot wire format beyond
  the additions described here: stop and escalate.
- Dependencies: any new runtime or development dependency: stop and escalate.
- Iterations: more than three focused repair attempts for one failing hosted
  job: stop and diagnose rather than weakening a gate.
- Hosted budget: at most one non-publishing hosted build and one warm-cache
  repeat for this milestone. A second full build needs a recorded decision.
- Ambiguity: if the planned `kani-snapshot-smoke` Makefile target name
  conflicts with the shipped dispatch target, record the conflict and use a
  distinct consumer target rather than silently renaming shipped behaviour.

The budgets were set after surveying the affected surfaces: EP-M4 added roughly
1,900 net lines across producer, workflow and tests, and EP-M3 added roughly
3,000 across the installer and its suites. This milestone adds two small pure
validators, four fixture crates, one opt-in integration module, one Makefile
target and one runbook, so 1,800 code lines and 1,600 documentation lines give
about 30 percent contingency over the estimate. EP-M5 and roadmap 1.1.5 are the
only entries in scope.

## Risks

- Risk: the first hosted non-publishing build fails in the upstream build or
  assembly steps. Severity: high. Likelihood: medium. Mitigation: preserve the
  build-failure artefact, read the recorded evidence, and repair only a
  demonstrated defect. Do not synthesize evidence from a failed run.
- Risk: the real Kani transcript format differs from the recorded fixture in
  whitespace, summary wording or exit status. Severity: medium. Likelihood:
  medium. Mitigation: the parser accepts the documented variants, the fixture
  is regenerated from a real run when it differs, and the integration test
  records the real transcript.
- Risk: the consumer smoke needs network access to the pinned Rust toolchain
  and to GitHub's attestation service. Severity: medium. Likelihood: low.
  Mitigation: record the exact failure and keep the test opt-in; a network
  failure is not a proof failure.
- Risk: the Makefile name planned for the consumer smoke
  (`kani-snapshot-smoke`) is already the dispatch target. Severity: low.
  Likelihood: certain. Mitigation: keep the shipped dispatch target and add
  `kani-snapshot-verify`; record the reconciliation in the parent plan's
  decision log and the operations runbook.
- Risk: the integration test's setup trace policy rejects a legitimate
  rustup helper or misses a source build. Severity: medium. Likelihood: low.
  Mitigation: test both an allowed trace and seeded forbidden traces offline,
  and record the real trace as evidence.

## Progress

- [x] (2026-11-22) Reconnaissance: read the parent plan's EP-M5/V8/D6 sections,
      the design, the users' and developers' guides, the workflow, the producer
      purity contract, the opt-in test idiom and the documentation gates.
- [x] (2026-11-22) Renamed the branch and set the Lody session title.
- [x] (2026-11-22) Dispatched the non-publishing hosted smoke build
      (`publish=false`) on `main` at upstream
      `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`, publication identity
      `kani-snapshot-2026-11-686f7753991b-r1`, run `45067008011`. Nothing is
      published.
- [ ] `EP-M5-A` Add the pure proof-transcript and setup-trace validators, their
      fixtures and offline tests.
- [ ] `EP-M5-B` Add the opt-in consumer smoke integration test and the
      `kani-snapshot-verify` Makefile target.
- [ ] `EP-M5-C` Write the operations runbook and synchronize the guides and
      design.
- [ ] `EP-M5-D` Download the hosted release, run the consumer smoke, record
      the evidence, and close EP-M5 and roadmap 1.1.5 only if it passes.
- [ ] Run every gate and create the draft PR.

## Surprises & discoveries

- Observation: the parent plan's planned Makefile target
  `make kani-snapshot-smoke` with `SNAPSHOT_PIN`, `SNAPSHOT_ASSET_DIR` and
  `SNAPSHOT_FIXTURE_ROOT` was written before EP-M4 shipped that name for the
  hosted dispatch. Evidence: `Makefile:97-127` and
  `tests/test_workflow_makefile_contract.py:132-273`. Impact: the consumer
  target is `kani-snapshot-verify`; the dispatch target keeps its shipped name.
  Both are documented together.
- Observation: no `kani-monthly.yml` run had ever executed before this
  milestone, and the repository has no tags or releases. Evidence:
  `gh run list --workflow kani-monthly.yml` and `gh release list`. Impact: this
  is the first real producer run, so its evidence is the first hosted
  observation rather than a comparison with a baseline.

## Decision log

- D1. Decision: keep `kani-snapshot-smoke` as the hosted non-publishing
  dispatch and add `kani-snapshot-verify` for the consumer-path smoke.
  Rationale: renaming the shipped target would break the documented operator
  command and its contract tests for no functional gain. The runbook names the
  two phases explicitly, and the parent plan records the reconciliation.
  Date/Author: 2026-11-22, agent.
- D2. Decision: the transcript validator requires an exact named harness set,
  accepts verification failure only for the deliberately false harness, and
  refuses a missing summary whose counts disagree with the observed harness
  sections. Rationale: "process exited non-zero" cannot distinguish a failed
  assertion from a compile error, a solver launch failure and a zero-harness
  run. The summary is a second, independent statement of the counts, so a
  contradiction is detectable. Date/Author: 2026-11-22, agent.
- D3. Decision: fixtures are ordinary Cargo crates under
  `tests/fixtures/kani_snapshot/proofs/` with no Kani dependency; `cargo kani`
  supplies the `kani` crate to proof attributes. Rationale: this matches an
  ordinary consumer crate and keeps the fixture buildable without a registry
  fetch. The crates are never compiled by `make test`. Date/Author: 2026-11-22,
  agent.
- D4. Decision: the setup-trace validator rejects any `cargo` invocation,
  `rustc` invocation other than a version or print probe, and any
  `cc`/`gcc`/`clang`/`ld`/`make`/`cmake`/`ninja` execution, while permitting
  `rustup toolchain install`, extraction tools, `gh attestation verify` and the
  launcher's own probes. Rationale: setup is allowed to download and install
  the prebuilt toolchain, but not to compile Kani, a solver or any other tool
  source. An allow-list that names only observed helpers would be bypassed by a
  new helper; the deny-list names the actions the design forbids. Date/Author:
  2026-11-22, agent.
- D5. Decision: the hosted smoke uses the builder merged on `main`; this
  branch adds only consumer-side validation, documentation and fixtures.
  Rationale: EP-M5 requires trusted-main hosted evidence. The builder path is
  unchanged by this milestone, so the dispatched run is representative.
  Date/Author: 2026-11-22, agent.

## Outcomes & retrospective

To be completed after the gates, the hosted consumer smoke and the runbook.

## Context and orientation

The consumer commands are registered in
`rust_prover_tools/kani_snapshot_cli.py` and implemented under
`rust_prover_tools/kani_snapshot/`. Installation stages bytes through
`install_transport.py` and `install_sender.py`, verifies the manifest and
attestation through `integrity.py` and `attestation.py`, extracts the backend
and launcher through `archive.py`, runs setup through `identity.py`, and
records the generation through `install_generation.py` and `activation.py`.

The producer package `rust_prover_tools/kani_snapshot/producer/` holds pure
decisions. Its purity contract is documented in that package's `__init__.py`
and enforced by `tests/test_kani_snapshot_producer_purity.py`. Existing
compiler-floor parsing is in `producer/compiler_floor.py`, with fixtures under
`tests/fixtures/kani_snapshot/compiler_floor/`.

The snapshot workflow is `.github/workflows/kani-monthly.yml`. Its job-level
contracts are pinned by `tests/test_kani_workflow_contract.py`,
`tests/test_kani_workflow_build_contract.py` and
`tests/test_kani_workflow_condition_contract.py`. The Makefile target
`kani-snapshot-smoke` dispatches a non-publishing run; its bytes are pinned by
`tests/test_workflow_makefile_contract.py`.

The opt-in test idiom already used in the repository is a module-level
`pytest.mark.skipif` on an environment variable, as in
`tests/test_kani_exec_codegen_backend.py`. `pyproject.toml` sets a 30-second
default timeout, so a real smoke test needs an explicit
`@pytest.mark.timeout(...)`.

Relevant documentation and skills:

- `docs/kani-snapshot-distribution-design.md`, especially D6.
- `docs/execplans/kani-monthly-snapshot-distribution.md`, especially EP-M5 and
  V8.
- `docs/users-guide.md`, `docs/developers-guide.md`,
  `docs/prover-tools-cli-design.md`.
- `docs/documentation-style-guide.md`.
- Skills: `kani`, `python-router`, `python-testing`, `hexagonal-architecture`,
  `codegraph-mcp`, `execplans`.

## Conformance basis

- `docs/kani-snapshot-distribution-design.md` revision 7, decision D6
  (evidence and operational handoff), D4 (installation and execution) and D2
  (authentication).
- `docs/execplans/kani-monthly-snapshot-distribution.md` revision 4,
  milestone EP-M5 and verification obligations V8 and V9 (V9 remains EP-O1).
- `docs/roadmap.md` item 1.1.5 and the 1.2.1 boundary.
- Requirements derived from those sources:
  - `REQ-transcripts`: exact named harness sets and counts, with positive,
    false-assertion, zero-harness, compile-error, solver-failure,
    malformed/truncated and contradictory controls.
  - `REQ-floor`: the effective compiler is at least Rust 1.94 and the floor
    harness verifies; the 1.93 control fails for the unavailable API.
  - `REQ-setup`: clean setup compiles no tool source, observed through a
    descendant `execve` trace.
  - `REQ-consumer`: cold install, check, proof, warm repeat and
    digest/identity rejection through the public commands.
  - `REQ-handoff`: an operations runbook with settings, dispatch, identity,
    recovery, rollback, retention and monitoring.
- Trace links:

```text
D6/EP-M5/V8 -> REQ-transcripts -> EP-M5-A -> tests/test_kani_snapshot_proof_report.py
D6/EP-M5/V8 -> REQ-floor       -> EP-M5-A -> tests/test_kani_snapshot_compiler_floor.py
D6/EP-M5/V8 -> REQ-setup       -> EP-M5-A -> tests/test_kani_snapshot_setup_trace.py
D6/EP-M5/V8 -> REQ-consumer    -> EP-M5-B -> tests/integration/test_kani_snapshot_smoke.py
D6/EP-M5    -> REQ-handoff     -> EP-M5-C -> docs/kani-snapshot-operations.md
```

## Verification plan

### Abstract model

A transcript is a sequence of events: a harness start naming one harness, a
terminal verdict for that harness, an optional summary naming verified, failed
and total counts, and zero or more tool diagnostics. A valid positive run maps
every required harness to exactly one successful verdict, has no diagnostics
that indicate a compiler or solver failure, and (when present) has a summary
whose counts equal the observed verdicts. A valid negative run maps the false
harness to exactly one failed verdict and has no compiler diagnostic.

A setup trace is a sequence of `execve` records. Each record has an executable
and an argument vector. The policy maps a record to allowed or forbidden. A
trace is valid when it contains at least one record and no forbidden record.

### Invariants and lemmas

- `INV-exact-set`: the reported verified and failed harness names equal the
  expected sets exactly; no missing, extra or duplicated verdict is accepted.
- `INV-summary`: when a summary is present, its verified, failed and total
  counts equal the counts derived from the harness verdicts.
- `INV-no-compile`: a transcript containing a compiler diagnostic or a
  `could not compile` message is refused even if a harness line precedes it.
- `INV-false-is-verification`: the negative fixture's failed verdict is
  accepted only when no compiler diagnostic explains it.
- `INV-no-tool-build`: the setup trace contains no `cargo` invocation, no
  code-generating `rustc`, and no C/C++ compiler, linker or build tool.
- `INV-bounded`: a transcript or trace larger than its fixed ceiling is
  refused without being read whole by the caller.
- `L-report`: if every event is well formed and the summary agrees, the
  parser's report is a total function of the events and is byte-stable when
  rendered.
- `L-refusal`: a seeded malformed event (missing verdict, duplicate verdict,
  truncated summary, wrong count) is refused with a message naming the harness
  or the count.

### Axioms (trusted, not verified here)

- `AXIOM-kani`: the effective Kani writes the documented `Checking harness`,
  `VERIFICATION:- SUCCESSFUL|FAILED` and `Manual Harness Summary` lines.
  Source: the Kani CLI output, recorded by the checked-in transcript fixtures.
- `AXIOM-strace`: `strace -f -e trace=execve` records every descendant
  `execve` with an executable path and argv. Source: the strace manual.
- `AXIOM-gh`: `gh attestation verify` refuses bytes that no trusted workflow
  signed. Source: the GitHub CLI attestation manual.
- `AXIOM-network`: the pinned Rust toolchain and the attestation service are
  reachable during the opt-in smoke. A failure here is recorded as an
  environment failure, not a proof result.

### Methods per obligation

- Obligation: `INV-exact-set` and `INV-summary`.
  Method: parameterized tests plus a Hypothesis property over generated harness
  verdict sets, with an independent expected-count oracle. Artefact:
  `rust_prover_tools/unittests/test_kani_snapshot_proof_report.py`. Evidence:
  `pytest -q rust_prover_tools/unittests/test_kani_snapshot_proof_report.py`.
  Non-vacuity: generated sets include zero, one and several harnesses, a
  false-only set and a mixed set; a mutation that drops one harness must fail.
- Obligation: `INV-no-compile` and `INV-false-is-verification`.
  Method: named fixture transcripts for compile error, solver failure,
  truncated output, contradictory summary and the real false-assertion
  transcript. Artefact: `tests/fixtures/kani_snapshot/transcripts/` and the
  same test module. Non-vacuity: each control asserts the specific error class,
  not merely that some exception was raised.
- Obligation: `INV-no-tool-build`.
  Method: parameterized `execve` traces, one allowed and one for each forbidden
  action (`cargo build`, `cargo install`, `rustc file.rs`, `cc`, `make`), plus
  a real trace from the consumer smoke. Artefact:
  `rust_prover_tools/unittests/test_kani_snapshot_setup_trace.py` and the
  integration test. Non-vacuity: the allowed trace must pass; each seeded
  forbidden record must be refused by name.
- Obligation: `INV-bounded`.
  Method: an oversize transcript and trace are refused before parsing.
  Artefact: both unit-test modules.
- Obligation: `REQ-consumer`.
  Method: opt-in end-to-end integration test against the hosted artefact
  directory, invoking the public console script in a fresh virtual environment.
  Artefact: `tests/integration/test_kani_snapshot_smoke.py` and
  `make kani-snapshot-verify`. Evidence: cold install, check, positive
  harnesses, false harness, floor harness, 1.93 control, warm reuse, corrupted
  payload, changed identity. Non-vacuity: the false harness must fail
  verification, the corrupted payload must be refused without an activation
  record, and the changed identity must be refused before setup.
- Obligation: `REQ-handoff`.
  Method: editorial review against the D6 and EP-M5 checklists, plus the
  Markdown and spelling gates. Artefact: `docs/kani-snapshot-operations.md` and
  the synchronized guides. Non-vacuity: every checklist item appears with a
  concrete command or value, and no claim names a setting that was not verified.

### Formal verification decision

No new model checking or formal proof is introduced. The change validates
observed tool output and documents an operational procedure; the expensive real
proof is supplied by Kani itself. Parameterized, property and end-to-end tests
are proportionate. Kani is used as the external verifier under test, not to
prove this repository's Python code.

### Suggested BDD features

The existing `tests/features/kani_snapshot.feature` covers installation. The
new consumer smoke is expressed as a named pytest integration test rather than
a new BDD feature, because the Gherkin runner would need to own a real asset
directory and a real toolchain install. Proposed acceptance scenarios, if a BDD
runner is later chosen:

```gherkin
Feature: Consumer verification of a produced snapshot
  Scenario: A produced snapshot verifies its named harnesses
    Given a release directory produced by a non-publishing hosted run
    When the consumer installs it through the public command
    Then the selected compiler is at least Rust 1.94
    And the named positive harnesses verify
    And the deliberately false harness fails verification rather than compilation
```

## Plan of work

### `EP-M5-A` Pure validators, fixtures and offline tests

Add `rust_prover_tools/kani_snapshot/producer/proof_report.py`. It defines a
frozen `HarnessOutcome` record, a frozen `ProofReport` record, a parser that
splits a transcript into harness sections, a summary parser, and
`require_named_harnesses(transcript, verified=..., failed=...)`. It raises the
existing `ProverToolError` subclasses or a small dedicated error whose message
names the offending harness. It enforces the fixed nesting and size ceilings.

Add `rust_prover_tools/kani_snapshot/producer/setup_trace.py`. It defines a
frozen `ExecRecord`, a bounded parser for `strace -f -e trace=execve` output,
and `require_prebuilt_setup(trace)` with the D4 deny-list. It returns the
observed records for evidence.

Add fixture crates under `tests/fixtures/kani_snapshot/proofs/`:
`bounded_collection` with the harnesses `bounded_push_and_drop_are_exact` and
`raii_release_runs_once`; `false_assertion` with
`deliberately_false_assertion`; and `compiler_floor` with
`bounded_adjacent_pairs_are_exact` using `array_windows::<2>()`.

Add tranche fixtures under `tests/fixtures/kani_snapshot/transcripts/` for the
positive set, the false assertion, a zero-harness run, a compile error, a
solver failure, a truncated transcript and a contradictory summary.

Add `rust_prover_tools/unittests/test_kani_snapshot_proof_report.py` and
`rust_prover_tools/unittests/test_kani_snapshot_setup_trace.py`, with
parameterized and Hypothesis coverage and Syrupy snapshots of the rendered
reports. Update `producer/compiler_floor.py` only if the real transcript
requires it.

Focused commands:

```bash
python scripts/uv_gate.py run -- pytest -q \
  rust_prover_tools/unittests/test_kani_snapshot_proof_report.py \
  rust_prover_tools/unittests/test_kani_snapshot_setup_trace.py
python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_producer_purity
```

### `EP-M5-B` Opt-in consumer smoke and Makefile target

Add `tests/integration/test_kani_snapshot_smoke.py`. It is gated by
`KANI_SNAPSHOT_SMOKE=1`, reads `KANI_SNAPSHOT_SMOKE_ASSET_DIR`,
`KANI_SNAPSHOT_SMOKE_FIXTURE_ROOT`, `KANI_SNAPSHOT_SMOKE_PROVER_TOOLS` and
`KANI_SNAPSHOT_SMOKE_RUSTC_1_93`, and runs the public commands. It builds a
consumer repository from the fixture crate, writes the pin from the manifest
and its digest, performs the cold install (under `strace` when available), and
then asserts the required evidence. It reuses the pure validators.

Add the `kani-snapshot-verify` Makefile target. It refuses missing variables,
builds or accepts a wheel, installs it into a temporary virtual environment,
and runs only the integration module with the environment variables set. It is
never part of `all`.

Focused command:

```bash
make kani-snapshot-verify \
  KANI_SNAPSHOT_ASSET_DIR=/path/to/release \
  KANI_SNAPSHOT_WHEEL=/path/to/wheel.whl
```

### `EP-M5-C` Operations runbook and documentation synchronization

Write `docs/kani-snapshot-operations.md` with the D6 checklist and the verified
commands. Update `docs/users-guide.md` with the verification target and the
compiler-floor and harness expectations. Update `docs/developers-guide.md` with
the validator modules, the fixture inventory, the harness inventory and the
integration-test environment variables. Update
`docs/prover-tools-cli-design.md` with the validation boundary. Update
`docs/kani-snapshot-distribution-design.md` D6 with the implemented evidence
and the target-name reconciliation. Update the parent plan's Progress,
Surprises and Decision log, and mark EP-M5 only after the hosted evidence.

### `EP-M5-D` Hosted non-publishing evidence and roadmap closure

Download the `kani-snapshot-release-<run>-<attempt>` artefact from the
dispatched run, build the consumer wheel, and run the opt-in smoke. Record in
this plan and the parent plan: the run URL, the publication identity, the
builder and upstream commits, the effective compiler, the harness names and
statuses, the cold and warm install results, the setup trace verdict, and the
two refusal cases. Mark roadmap 1.1.5 and parent EP-M5 complete only when every
item is observed. Keep EP-O1 and roadmap 1.2.1 open.

## Milestones and plateaus

- `EP-M5-A`: pure validators, fixtures and offline tests.
  Requirements: `REQ-transcripts`, `REQ-floor`, `REQ-setup`. Acceptance: the
  focused suites pass and a seeded defect is refused. Conformance: producer
  purity holds; no public interface changes; fixtures are untracked by the
  build. Recovery: revert the module and fixture commit. Remaining: the
  integration harness and documentation. Compatibility: none.
- `EP-M5-B`: opt-in consumer smoke and target.
  Requirements: `REQ-consumer`. Acceptance: the module is skipped by default
  and passes against a real release; the target refuses missing inputs.
  Conformance: ordinary `make test` still performs no real build; the shipped
  dispatch target is unchanged. Recovery: rerun with a fresh cache; the target
  owns no persistent state. Remaining: the runbook and hosted evidence.
  Compatibility: none.
- `EP-M5-C`: runbook and synchronized guides.
  Requirements: `REQ-handoff`. Acceptance: the Markdown, spelling and Mermaid
  gates pass; every D6 checklist item is present. Recovery: ordinary
  documentation revert. Remaining: hosted evidence. Compatibility: none.
- `EP-M5-D`: hosted evidence and closure.
  Requirements: all of the above. Acceptance: the non-publishing run and
  consumer smoke both pass and the evidence is recorded. Recovery: keep the
  schedule disabled and preserve the artefacts. Remaining: EP-O1.
  Compatibility: none.

## Concrete steps and acceptance commands

The expected full gate sequence, run sequentially with `FORCE_COLOR` unset:

```bash
env -u FORCE_COLOR make fmt
env -u FORCE_COLOR make check-fmt
env -u FORCE_COLOR make lint
env -u FORCE_COLOR make typecheck
env -u FORCE_COLOR make test
env -u FORCE_COLOR make markdownlint
env -u FORCE_COLOR make nixie
env -u FORCE_COLOR make workflow-check
```

Smoke evidence belongs in this section and in the parent plan's Progress. Full
traces stay under `/tmp` and are cited by path; only bounded excerpts are
recorded.

## Idempotence and recovery

The unit and property tests are pure and rerunnable. The integration test
creates a fresh temporary consumer root and cache per run, so a failed run
leaves no shared state. A warm repeat reuses the generation the same run
installed. The hosted run is a single dispatch; a failed run is diagnosed from
its artefacts and, if the cause is a repository defect, a new attempt uses the
same publication identity only when the run and attempt rules allow it, or a
new identity otherwise. No command in this plan publishes or mutates a
repository setting.

## Artefacts and notes

- Hosted run:
  <https://github.com/leynos/rust-prover-tools/actions/runs/45067008011>
- Publication identity: `kani-snapshot-2026-11-686f7753991b-r1`
- Upstream commit: `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`
- Builder commit: the `main` HEAD at dispatch time.

## Revision note

This is the first revision. It records the reconnaissance, the branch rename,
the non-publishing dispatch and the implementation plan for EP-M5. No
implementation outcome is claimed until the gates and the hosted consumer smoke
have run.
