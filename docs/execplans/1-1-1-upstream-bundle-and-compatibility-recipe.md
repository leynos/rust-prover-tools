# Establish the upstream bundle and compatibility recipe

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE

Branch: `1-1-1-upstream-bundle-and-compatibility-recipe`

Roadmap item: `1.1.1` in `docs/roadmap.md`.

## Purpose / big picture

This repository will publish prebuilt Kani snapshots for
`x86_64-unknown-linux-gnu`, so a consumer can install a verifier instead of
compiling Kani and its solvers. A snapshot is only trustworthy if the recipe
that produces it is pinned: one upstream release, one toolchain, one builder
image, one bundle layout, one launcher build, one runtime and licence
inventory. This plan established and demonstrated that recipe.

Before this work, the project had approved a design and a roadmap but had never
built a Kani bundle. In particular nobody had shown which upstream commit to
use, what the bundle contains, whether local setup accepts it, or which Rust
compiler is the true floor. After this work, `pinned_recipe()` in
`rust_prover_tools/kani_snapshot/producer/recipe.py` returns a validated
description of the whole build, its canonical JSON is snapshotted, and every
pinned value is checked against an upstream file or a captured transcript. A
prototype bundle was built, its launchers were packaged and installed, local
setup accepted both, and a Kani harness that needs Rust 1.94 verified.

Nothing is published by this work. EP-M4 (publication automation) and EP-M5
(clean proof and handoff) remain in the parent plan
`docs/execplans/kani-monthly-snapshot-distribution.md`.

The terms used here:

- *snapshot*: a centrally built, immutable Kani verifier for one upstream
  release and one target.
- *bundle*: the `kani-<version>-<target>.tar.gz` archive that upstream's
  `cargo bundle` produces, containing `bin/`, `lib/`, libraries and metadata.
- *launcher*: the small `kani` and `cargo-kani` executables that dispatch to an
  installed bundle.
- *producer*: the repository-side builder that creates the snapshot; its domain
  package is `rust_prover_tools/kani_snapshot/producer`.
- *compiler floor*: the oldest Rust release that can compile the harnesses the
  snapshot promises to support. This plan demonstrates Rust 1.94.

## Constraints

- The producer package is pure domain code. It must not start subprocesses,
  read the environment, use the network or write files. Adapters receive its
  argument vectors.
- The pinned upstream identity is the commit that the released `kani-0.68.0`
  tag points at, never the head of upstream `main`.
- No publishing, signing, credential or release-workflow change belongs here.
- Existing consumer behaviour (`prover-tools kani install`, snapshot install,
  check and run) must not change.
- The producer package must not be named `build`. `.gitignore` ignores every
  `build/` directory, and the shared code indexer skips those names too.
- Python style follows `pyproject.toml`, `.rules/` and `AGENTS.md`: Ruff line
  length 88, docstrings on every module, class and function, Pylint's 400-line
  module limit for package modules, and modules below Ruff's complexity limit.
- Every gate listed in `AGENTS.md` must pass before a commit: `make check-fmt`,
  `make typecheck`, `make lint`, `make test`, and for documentation
  `make markdownlint` and `make nixie`.

## Tolerances (exception triggers)

These thresholds define the boundary of autonomous work. Implementation is
complete, so they are recorded as the limits that were observed rather than
expected. ExecPlan files are excluded from all size counts.

- Files: stop if more than 20 non-ExecPlan files change.
- Code: stop if code changes exceed 2,500 net lines. Code covers the producer
  package, unit tests and test fixtures, including transcripts.
- Configuration: stop if configuration changes exceed 50 net lines. No
  configuration file was intended to change.
- Documentation: stop if documentation changes exceed 400 net lines, excluding
  ExecPlan files.
- Interface: stop if a consumer-facing API or persisted format changes.
- Dependencies: stop if a new runtime or development dependency is needed.
- Architecture: stop if the domain/adapter split in the design is abandoned.
- Iterations: stop after three failed attempts at one failing gate.
- Time: stop if a single milestone exceeds eight hours of wall-clock work.

No comparable completed ExecPlan in this repository had its budgets raised, so
these numbers are a first estimate anchored on the size of the existing
`rust_prover_tools/kani_snapshot` package and its tests.

Recorded deviation (2026-11-15): the delivered change exceeds two of those
budgets. Counts against base `77b46af`, measured with `git diff --numstat`,
excluding both ExecPlan files, and re-measured after the formal-review repairs:

- Files: 29 non-ExecPlan files changed against a budget of 20.
- Code: 4,622 net lines across `rust_prover_tools/` and `tests/` against a
  budget of 2,500.
- Documentation: 129 net lines against a budget of 400, so within budget.
- Configuration, interface and dependencies: no change, as intended.

The excess is evidence rather than production logic: the pinned upstream files,
the two captured transcripts, the tag ref, and the tests that bind each pinned
value to them. The requirement for that evidence arrived with the assignment,
so an estimate made before it existed was too low. See D11.

Escalation record (2026-11-15): the overrun and these counts were reported to
the plan's owner and maintainer when they were measured, and the work was not
trimmed to fit. The owner's three options are to accept the exceeded limits, to
raise them in a plan revision that keeps these original figures, or to reject
the delivery. This plan defines no separate approval artefact, so a reviewer
looking for written acceptance should read this record and the owner's
assignment, not a waiver attached elsewhere.

## Risks

- Risk: a bundle built on the developer host links a newer glibc than the
  published target can supply. Severity: high. Likelihood: high. Mitigation:
  record the builder image and its glibc in the recipe, refuse a runtime floor
  newer than the builder, and document that a local measurement is prototype
  evidence only.
- Risk: the upstream notice inventory omits a redistributed component.
  Severity: high. Likelihood: medium. Mitigation: compare the bundle's binaries
  with `license-notes.txt` and add a component licence where upstream is
  silent; the recipe records the evidence kind per component.
- Risk: the recipe is pinned to post-release `main` rather than the release.
  Severity: high. Likelihood: medium. Mitigation: resolve the annotated tag to
  its commit and check the crate version, toolchain and published time.
- Risk: one module outgrows the 400-line Pylint limit.
  Severity: low. Likelihood: high. Mitigation: separate frozen records from
  their invariants before adding more rules.
- Risk: a full verification transcript is too large to review.
  Severity: low. Likelihood: medium. Mitigation: capture the positive run with
  Kani's terse output format.

## Progress

- [x] (2026-11-15) Resolve the released tag `kani-0.68.0` to
      `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`, confirm crate version,
      published time, toolchain channel and required upstream Linux checks, and
      replace every planning-time commit reference.
- [x] (2026-11-15) Capture the upstream evidence fixtures under
      `tests/fixtures/kani_recipe`: `rust-toolchain.toml`, `Cargo.toml`,
      `kani-dependencies`, `gitmodules`, `license-notes.txt`, the release and
      check-run responses, and the recursive gitlinks.
- [x] (2026-11-15) Add the producer package: records, invariants, canonical
      JSON, strict parser, upstream-file parsers, resolution policy, argument
      vectors and the compiler-floor transcript checks.
- [x] (2026-11-15) Demonstrate the compiler floor: 1.93.1 rejects
      `array_windows` with `E0658`, 1.94.0 and the pinned nightly accept it, and
      the pinned toolchain verifies the named harness.
- [x] (2026-11-15) Build the prototype bundle, package and install the
      launchers, create the launcher archive, run local setup against the
      pinned toolchain, and verify the floor harness.
- [x] (2026-11-15) Compose the notices bundle and measure the runtime and
      licence inventories.
- [x] (2026-11-15) Record the design decisions and internal conventions in the
      design document, the parent execution plan, the developer guide and the
      roadmap.
- [x] (2026-11-15) Run the full commit gates and deliver the branch in draft
      pull request #254.
- [x] (2026-11-15) Request the ExecPlan completeness assessment
      (`EPM1-254-20261115T1800Z`) at head `7dee339` and repair its five in-scope
      findings: read the released commit from the tag ref rather than the
      release's `target_commitish`, refuse unknown and decorated launcher
      template fields, reject a boolean `schema_version`, require a non-empty
      duplicate-free submodule list, and bind each transcript verdict to its own
      harness line. Each repair carries a test that fails against the reviewed
      head.
- [x] (2026-11-15) Close the second assessment round at head `2da5616`: the
      submodule rule accepted a sorted, non-empty subset of the pinned release's
      gitlinks, so a document could drop a submodule and still pass validation
      and the parser. Records the pinned gitlinks as data in `recipe_evidence`,
      requires an exact match for the pinned commit, and adds three tests
      (partial list, moved commit, parser gate). Corrects the recorded file
      count to the independently confirmed figure.
- [x] (2026-11-15) Close the formal CodeRabbit review of head `577cbcb` by
      repairing the findings it accepted: a tie between check runs is refused
      only when the tied runs disagree, and an unstarted rerun counts as the
      newest observation so a queued rerun blocks qualification; the negative
      control must report `array_windows` in its own `E0658` diagnostic; a
      boolean compiler-floor component is refused like a boolean
      `schema_version`; and the runtime baseline must equal the builder glibc,
      not merely be no newer than it. Renumbers the roadmap's footnotes by
      first appearance and corrects its focused-test filter.
- [x] (2026-11-15) Reconcile the formal review's failed pre-merge checks. The
      Testing (Overall) row's branch-level tests are added: runtime loader and
      library rules, submodule containment, ordering and digests on an
      unpinned commit, attestation prefix and blank arguments, prerequisite
      and licence duplicates and digests, upstream formats, nested parser
      groups, prerelease and non-integer check-run identifiers, metadata
      repeats and invalid UTF-8, licence-evidence source and casing, and the
      whole toolchain vector. The Domain Architecture warning is dispositioned
      rather than refactored (D14), and the constructor claim it identified is
      corrected in the module docstring and the guide.
- [x] (2026-11-15) Clear the hosted-only failure on head `a447733`:
      `test_a_timed_out_staging_worker_keeps_its_claim` asserted the staging
      claim after releasing the stalled copy, so a worker that finished first
      closed the claim and the test read that as a product defect. The
      assertion now runs while the copy is still blocked. Twenty focused runs
      pass.
- [x] (2026-11-15) Re-check the head after the fix: hosted `lint-test` and
      `Gecko Security Review` both pass on `58ddf74`, and
      `mergeStateStatus` is `CLEAN`. CodeRabbit paused automatic reviews after
      the run of pushes and reports that it last reviewed `577cbcb`, so its
      pre-merge rows and its `CHANGES_REQUESTED` decision still describe that
      commit. The single review queued for this PR through `comenq`
      (`4f0725c9`) is the outstanding item; its ETA is measured in hours
      because the queue is shared, so the branch waits on it rather than
      asking a second time.

## Surprises & discoveries

- Observation: the producer package was invisible to Git. `.gitignore` ignores
  `build/` at any depth, so a package named
  `rust_prover_tools/kani_snapshot/ build` never appeared as untracked, and the
  code indexer skips that name too. Evidence:
  `git check-ignore -v rust_prover_tools/kani_snapshot/build/recipe.py` reports
  `.gitignore:11:build/`. Impact: the package was renamed to `producer` and the
  reason is recorded in the developer guide.
- Observation: the host-built `kani-driver` needs `GLIBC_2.39`, not the
  compiler's `GLIBC_2.34`. Evidence: `readelf --version-info` over the bundle's
  `bin/` directory. Impact: the builder image is part of the recipe and a local
  runtime measurement cannot become a distribution claim.
- Observation: upstream's `license-notes.txt` lists Kani, rustc and CBMC but
  not Kissat, although `bin/kissat` ships in the bundle. Impact: the producer
  adds Kissat's MIT licence and records it as `component-licence` evidence.
- Observation: `kani-compiler` carries
  `RUNPATH ...:$ORIGIN/../toolchain/lib`, while an absolute developer-toolchain
  path is also embedded. Impact: setup's `toolchain` symlink is what makes the
  bundle relocatable, so a consumer must preserve it.
- Observation: `rust-toolchain-version` has no trailing newline. Impact:
  parsers must not assume one; the recipe stores the stripped value.
- Observation: a GitHub release object does not carry the commit it names.
  Evidence: `target_commitish` is `main` for `kani-0.68.0`, and only
  `refs/tags/kani-0.68.0` resolves to `0d2328a9…`. Impact: the parser takes a
  separately fetched tag ref, and refuses an annotated tag it would otherwise
  record as the commit.
- Observation: the initial positive transcript was 2,109 lines of CBMC checks.
  Evidence: `kani positive.rs` output. Impact: recapture with
  `--output-format terse`, which yields 29 lines and still names the harness
  and the verdict.
- Observation: `test_a_timed_out_staging_worker_keeps_its_claim` is
  order-dependent on its own events, not on the product. Evidence: it passed
  locally and on head `aa76d5a`, then failed on hosted CI for head `a447733`
  with "a timed-out worker released its staging claim". Impact: the test
  released the stalled copy in `finally` and only then asked whether the claim
  was held, so a worker that finished quickly closed its own claim first. The
  assertion now runs while the copy is still blocked, matching its sibling
  `test_an_abandoned_staging_copy_releases_its_claim_when_it_stops`, and twenty
  consecutive focused runs pass.

## Decision log

- D1. Decision: pin the release tag's commit
  `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`, not the planning-time
  `4e31125fdb4f01fad95150415cbafe46e0587d66`. Rationale: a snapshot must be
  built from the source the release came from; the rejected commit is
  post-release `main` with a newer nightly. Date/Author: 2026-11-15, agent.
- D2. Decision: the recipe records
  `BuilderRecord(image="ubuntu-22.04", glibc="2.35")` and validation refuses a
  runtime floor newer than the builder's glibc. Rationale: the measured
  `GLIBC_2.39` requirement of a Rocky-built driver would otherwise silently
  contradict the published claim; upstream's 22.04 bundle and setup jobs are
  the evidence that 2.35 is sufficient. Date/Author: 2026-11-15, agent.
- D3. Decision: split the recipe into `recipe_model` (frozen records) and
  `recipe_rules` (invariants). Rationale: the single module exceeded Pylint's
  400-line limit, and separating records from rules keeps each module's role
  legible. Date/Author: 2026-11-15, agent.
- D4. Decision: name the package `producer`, not `build`. Rationale: see the
  first surprise; a source package under an ignored name never reaches version
  control or the index. Date/Author: 2026-11-15, agent.
- D5. Decision: capture the positive compiler-floor transcript with
  `--output-format terse`. Rationale: it preserves the version header, the
  named harness and the verdict while removing thousands of repeated checks.
  Date/Author: 2026-11-15, agent.
- D6. Decision: add a Syrupy snapshot of the canonical recipe JSON in addition
  to the byte-stability and digest tests. Rationale: the digest proves that
  bytes changed, but the snapshot shows a reviewer which pin, digest or licence
  moved. Date/Author: 2026-11-15, agent.
- D7. Decision: no pytest-bdd feature for this milestone. Rationale: the
  package has no user-facing command or interaction; its observable contract is
  data. The repository does have pytest-bdd (`tests/features/`,
  `tests/steps/`), and consumer behaviour scenarios remain appropriate for
  EP-M4/EP-M5. Date/Author: 2026-11-15, agent.
- D8. Decision: add Kissat's licence as a component notice rather than editing
  upstream's `license-notes.txt`. Rationale: the upstream file is evidence and
  must stay verbatim; the producer composes the redistributed notices from it
  and adds the missing component. Date/Author: 2026-11-15, agent.
- D9. Decision: parse the GitHub API's own shapes and take the released commit
  from `refs/tags/<tag_name>`, refusing an annotated tag. Rationale: the review
  of head `7dee339` showed the fixtures had been normalized by hand, so the
  documented input boundary — a release object whose `target_commitish` is the
  branch `main` — was never exercised. The tag ref is the only object that
  resolves a tag to the commit it names. Date/Author: 2026-11-15, agent.
- D10. Decision: fail closed on three value shapes the review found: launcher
  template fields that are not exactly `{version}` and `{target}`, a boolean
  `schema_version`, and an empty or duplicate-bearing submodule list.
  Rationale: each escaped the promised `ProverToolError` contract, and the
  parser already refused the boolean, so validation and parsing disagreed about
  which recipes are well formed. Date/Author: 2026-11-15, agent.
- D11. Decision: record, rather than avoid, the size-tolerance deviation. The
  delivered diff changes more non-ExecPlan files and more net code, test and
  fixture lines than `Tolerances` allows. Rationale: the evidence the user
  required — pinned upstream files, two captured transcripts, a tag ref and the
  tests that bind them — is what exceeded the budget, and the work was
  delivered in full rather than trimmed to fit an estimate made before the
  evidence existed. The deviation and its final counts are recorded under
  `Tolerances`. Date/Author: 2026-11-15, agent.
- D12. Decision: keep the pinned release's gitlinks as plain data in
  `recipe_evidence`, and make `validate_recipe` require an exact match for the
  pinned commit. Rationale: the earlier rule only rejected empty and duplicated
  lists, so a sorted subset of the pinned tree's gitlinks passed validation and
  the parser's gate with it. Validation is the only gate a document passes, so
  it cannot reach for a fixture or a checkout; the evidence has to be data. The
  pinned recipe builds its records from the same tuple, so the rule and the
  recipe share one source and cannot drift. Date/Author: 2026-11-15, agent.
- D13. Decision: refuse a runtime baseline that differs from the builder glibc
  in either direction, and treat an unstarted check run as the newest
  observation of its name. Rationale: the formal review of head `577cbcb`
  observed that `baseline < builder` also publishes an unexercised claim — the
  build ran against `2.35`, so a published `2.31` floor is a promise nothing
  measured — and that the previous tie-break silently kept a completed success
  while its rerun was still queued, so a fresh run's result could never
  displace it. Runs that share the newest identity but report different results
  are still refused rather than resolved by order, because that disagreement
  means the evidence itself is ambiguous. Date/Author: 2026-11-15, agent.
- D14. Decision: keep the pure API and file parsers and the argv builders in
  the producer package, and define that package's boundary by effects rather
  than by data shape. Rationale: CodeRabbit's Domain Architecture row proposed
  moving `candidate_from_api`, the attestation prefix, `commands`,
  `upstream_metadata` and the JSON codec into adapters. Every one of them is a
  pure function: none performs I/O, so moving them changes no dependency
  direction and buys no testability, while the fail-closed checks would move
  away from the frozen records they protect. The attestation `command_prefix`
  is a recipe fact precisely so that an adapter cannot choose the command the
  freeze exists to pin. The only effectful code is the consumer that executes
  the vectors. Date/Author: 2026-11-15, agent.

## Outcomes & retrospective

The milestone is met. The released upstream identity is pinned and evidenced;
the recipe is a validated domain object rendered as byte-stable JSON whose
digest later milestones will record; the compiler floor is demonstrated by two
transcripts rather than inferred; the runtime and licence inventories are
measured; and the whole path from `cargo bundle` through launcher packaging,
local setup and verification was exercised on a prototype.

Gaps, deliberate and recorded: the prototype bundle itself is not committed or
published, and it was built on the wrong distribution for a release, so its
`GLIBC_2.39` driver is prototype evidence only. EP-M4 must rebuild on
`ubuntu-22.04`, and EP-M5 must validate on a clean host, including safe
re-extraction during setup. Build cost beyond this prototype run is unrecorded.

Lesson: before creating a new package directory, check `git check-ignore`. A
silently ignored source tree looks exactly like a clean working tree.

Retrospective on review: the completeness assessment found five real defects
that the passing suite did not, and every one was a place where a boundary
accepted a shape it had not checked — the release identity, the launcher
template, the schema version, the submodule list, and the harness verdict. The
lesson is that fixtures copied from a real API response carry the API's actual
shape; had the release fixture kept the invented `target_commitish` of a commit
SHA, the tag-ref boundary would never have been written.

## Context and orientation

The producer package lives in `rust_prover_tools/kani_snapshot/producer/`:

- `recipe_model.py` holds the frozen slotted records and the shared constants
  (`RECIPE_SCHEMA_VERSION`, required prerequisites, launcher binaries, licence
  components and evidence kinds).
- `recipe_rules.py` holds `validate_recipe`, the only constructor gate.
- `recipe.py` fixes the pinned values, emits canonical JSON and digests it.
- `recipe_parse.py` parses that JSON back strictly.
- `upstream_metadata.py` parses upstream files the recipe must agree with.
- `resolution.py` selects an upstream commit or validates a manual full SHA.
- `commands.py` derives argument vectors and asset names.
- `compiler_floor.py` states the floor and validates the two transcripts.

The consumer package `rust_prover_tools/kani_snapshot/` (pins, manifest,
download, install, run) is unchanged. Tests are
`rust_prover_tools/unittests/test_kani_snapshot_producer.py`,
`rust_prover_tools/unittests/test_kani_snapshot_compiler_floor.py` and the
Syrupy snapshot
`rust_prover_tools/unittests/__snapshots__/test_kani_snapshot_producer.ambr`.
Evidence lives under `tests/fixtures/kani_recipe/` and
`tests/fixtures/kani_snapshot/compiler_floor/`.

Relevant documentation and skills: `docs/kani-snapshot-distribution-design.md`
(D3), `docs/execplans/kani-monthly-snapshot-distribution.md` (EP-M1),
`docs/developers-guide.md` ("Kani snapshot producer recipe"), and the
`hexagonal-architecture`, `python-router`, `execplans` and `kani` skills.

The inspected base revision was `77b46af` on
`1-1-1-upstream-bundle-and-compatibility-recipe`. Locate symbols with
`rg -n 'def pinned_recipe|def validate_recipe' rust_prover_tools`.

## Conformance basis

Upstream artefacts: `docs/kani-snapshot-distribution-design.md` revision 7,
`docs/execplans/kani-monthly-snapshot-distribution.md` milestone EP-M1, and
roadmap item 1.1.1. No ADR covers this area; the design document is the
technical design.

Requirements and gaps used for traceability:

- `REQ-identity`: the recipe names one released upstream commit, its tag, crate
  version, publication time, toolchain channel and rustc version.
- `REQ-floor`: the supported compiler floor is at least Rust 1.94 and no newer
  than the pinned compiler, and is demonstrated by execution.
- `REQ-runtime`: the runtime baseline is measured per binary and never newer
  than the builder image.
- `REQ-licence`: every redistributed component has licence evidence, including
  components upstream's inventory omits.
- `REQ-launcher`: the launcher build, archive layout and setup mechanism match
  the released upstream workflow.
- `GAP-build`: no bundle had ever been built or installed.

Trace:

```plaintext
roadmap 1.1.1 -> EP-M1 -> producer.recipe -> test_canonical_json_matches_the_reviewed_snapshot
REQ-identity -> producer.recipe -> test_pinned_recipe_matches_upstream_evidence
REQ-floor    -> producer.compiler_floor -> tests/test_kani_snapshot_compiler_floor.py
REQ-runtime  -> producer.recipe_rules._validate_builder -> test_validation_rejects_a_runtime_floor_newer_than_the_builder
REQ-licence  -> producer.recipe_rules._validate_licences -> test_pinned_recipe_matches_upstream_evidence
REQ-launcher -> producer.commands -> test_launcher_commands_match_the_upstream_workflow
```

## Verification plan

### Abstract model

The producer's observable output is a JSON document with eleven groups:
`attestation`, `builder`, `compiler_floor`, `launcher`, `licences`,
`prerequisites`, `runtime`, `schema_version`, `submodules`, `target` and
`upstream`. Rendering is a pure function of the pinned values; parsing is its
strict inverse; validation accepts exactly the documents whose values are
internally consistent, and, **for the pinned commit only**, consistent with the
upstream evidence. Evidence for any other commit is the caller's obligation:
`_require_pinned_gitlinks` skips the pinned-tree comparison when the upstream
commit differs, because the domain holds no evidence for that tree, so a future
producer must establish it before treating such a recipe as verified. Malformed
input, unknown or duplicate keys and every invariant violation raise
`ProverToolError`.

### Invariants and lemmas

- `INV-identity`: `upstream.tag == "kani-" + upstream.crate_version`, the commit
  is a full lowercase SHA, `published_at` is ISO-8601 UTC and the toolchain
  channel is dated. Discharged for the pinned values against
  `tests/fixtures/kani_recipe`.
- `INV-digest`: `recipe_to_json` is deterministic and byte-stable, and the
  digest is a function of those bytes alone.
- `INV-floor`: `1.94 <= compiler_floor <= pinned rustc`, and the floor is
  demonstrated by the positive and negative transcripts.
- `INV-runtime`: `runtime.glibc_baseline <= builder.glibc`, the shared-library
  list is sorted and unique, and each name is a plain library name.
- `INV-licence`: every component in `REQUIRED_LICENCE_COMPONENTS` appears
  exactly once, with a licence, a source and a known evidence kind.
- `INV-resolution`: a candidate qualifies only when every required check's
  newest run is completed and successful; ambiguous runs are refused.
- Lemma `L-parse`: `parse_recipe(recipe_to_json(r)) == r` for every valid `r`,
  so later milestones can consume the document as the recipe's identity.

### Axioms (trusted, not verified here)

- `AXIOM-upstream-build`: `cargo bundle -- <version>` produces the layout the
  prototype observed; the claim is bounded by one prototype build.
- `AXIOM-cbmc`: CBMC's `ubuntu-22.04` package needs no symbol newer than
  `GLIBC_2.34`; measured with `readelf`, not proved.
- `AXIOM-gh`: `gh attestation verify` accepts exactly the documented argument
  prefix; only the frozen prefix and verifier identity are modelled.
- `AXIOM-rustc`: Rust 1.93.1's stabilization boundary for `array_windows` is
  what the negative transcript records.

### Methods per obligation

- Obligation: `INV-identity`, `INV-runtime`, `INV-licence`, `INV-resolution`.
  Method: parameterized example tests plus golden upstream fixtures. Rationale:
  the contract is a finite set of value relations and named cases. Domain: the
  pinned recipe, the checked-in upstream files, and targeted mutations of each
  field. Artefact: `test_kani_snapshot_producer.py`. Evidence:
  `pytest rust_prover_tools/unittests/test_kani_snapshot_producer.py` passes 61
  tests, and each rejection test fails when its check is removed. Non-vacuity:
  acceptance and rejection pairs exist for every validator, and the pinned
  recipe is a satisfying witness.
- Obligation: `INV-digest`, `L-parse`.
  Method: Hypothesis property tests over generated identities plus the round
  trip over the pinned recipe. Rationale: these are universally quantified
  relations over cheap inputs. Domain: generated commits, versions and
  toolchain channels; digest-mutation cases. Artefact: the same module.
  Evidence: 50 generated examples per property pass; the mutation loop asserts
  a changed digest for each field change. Non-vacuity: the generator produces
  accepted values, and the hand-mutated documents must all fail.
- Obligation: `INV-floor`.
  Method: captured transcripts checked by string contracts, plus a property
  test over compiler versions. Rationale: the floor is a claim about real
  compiler behaviour, and the evidence must be the real output. Domain: the two
  checked-in transcripts and generated major/minor pairs. Artefact:
  `test_kani_snapshot_compiler_floor.py`. Evidence: 14 tests pass; removing the
  harness line from the positive transcript makes the validator refuse it.
  Non-vacuity: the positive transcript names the harness and reports success,
  the negative one reports `E0658`, and the transcript for another error is
  refused.
- Obligation: document shape stability.
  Method: Syrupy approval snapshot. Rationale: it makes an unintended pin
  change visible as a reviewed diff. Artefact: the snapshot file. Evidence: the
  snapshot passes and regenerates only under `--snapshot-update`. Non-vacuity:
  the snapshot records the full document rather than a digest.

### Formal verification decision

No model checking or formal proof is used. The producer is pure data validation
and rendering with no concurrency, arithmetic or transition system; example,
property and approval tests cover its obligations, and the executed prototype
covers the external build and setup boundary. A formal tool would add cost
without a stronger claim about third-party build behaviour, which remains an
explicit axiom. Recorded as D6 and in the parent plan.

### Suggested BDD features

Not adopted, per D7. There is no user-facing interaction in this milestone. The
consumer scenarios already in `tests/features/kani_snapshot.feature` exercise
install, check and run; producer scenarios become meaningful when EP-M4 adds
the workflow and publication commands.

## Plan of work

The work proceeded as four coherent plateaus, each leaving the tree valid.

`EP-M1` identity and evidence: resolve the release tag, capture the upstream
files and API responses as fixtures, and assert the recipe against them. The
first red test compares the pinned commit with the fixture and fails until the
release resolution is corrected.

`EP-M2` recipe domain: add records, rules, canonical rendering, strict parsing,
upstream-file parsers, resolution and command builders, with a test added
before each unit of behaviour.

`EP-M3` compiler floor: write the harness and the control source, capture both
transcripts from real compilers, and validate the transcripts.

`EP-M4` prototype execution and documentation: build the bundle, package and
install the launchers, run setup, verify the harness, compose notices, record
the design and conventions, and update the roadmap.

## Milestones and plateaus

- `EP-M1` outcome: the pinned identity and its evidence agree.
  Requirements: `REQ-identity`, `GAP-build` advanced. Acceptance evidence: the
  fixture comparison test and `tests/fixtures/kani_recipe/*`. Conformance:
  identity requirements satisfied; design D3 followed; upstream assumptions
  still valid; no public interface change; no trust-boundary change; trace
  links current. Recovery: re-fetch the upstream files from the same commit;
  never edit them. Remaining gaps: none for identity.
- `EP-M2` outcome: a validated, byte-stable recipe document.
  Requirements: `REQ-runtime`, `REQ-licence`, `REQ-launcher`. Acceptance
  evidence: the producer test module and the Syrupy snapshot. Conformance: all
  recipe invariants checked; domain stays pure; no consumer behaviour touched;
  no new dependency. Recovery: `pytest --snapshot-update` only for an intended
  pin change. Remaining gaps: publication wiring (EP-M4).
- `EP-M3` outcome: the compiler floor is demonstrated.
  Requirements: `REQ-floor`. Acceptance evidence: the two transcripts and
  `test_kani_snapshot_compiler_floor.py`. Conformance: floor between 1.94 and
  the pinned compiler; transcripts unedited; trace links current. Recovery:
  recapture with the documented commands if the floor changes. Remaining gaps:
  none.
- `EP-M4` outcome: the prototype path is exercised and documented.
  Requirements: `GAP-build`. Acceptance evidence: the setup transcript,
  `kani --version`, the harness verdict, and the document updates. Conformance:
  no publication; no committed binary; builder constraint recorded. Recovery:
  rebuild from the same tag; discard the prototype directory. Remaining gaps:
  release build on `ubuntu-22.04`, publication, clean-host validation.
- Compatibility decision: none. This work adds a new private package and does
  not change a released interface or a persisted format.

## Concrete steps

All commands run from the repository root.

```bash
# Focused tests (green)
env -u FORCE_COLOR make test

# Focused producer tests
env -u FORCE_COLOR uv run pytest \
  rust_prover_tools/unittests/test_kani_snapshot_producer.py \
  rust_prover_tools/unittests/test_kani_snapshot_compiler_floor.py -q

# Snapshot refresh, only for an intended pin change
env -u FORCE_COLOR uv run pytest \
  rust_prover_tools/unittests/test_kani_snapshot_producer.py \
  --snapshot-update

# Documentation gates
make markdownlint
make nixie
```

The prototype commands are recorded in the parent plan and in
`Artefacts and notes`; they need a Kani checkout, CBMC and Kissat on `PATH`,
and a private working directory outside `/tmp`.

## Validation and acceptance

- `make check-fmt`, `make typecheck`, `make lint`, `make test`,
  `make markdownlint` and `make nixie` pass.
- The two producer test modules report 75 passed.
- `pinned_recipe()` raises for any inconsistent edit, and
  `parse_recipe(recipe_to_json(pinned_recipe()))` returns an equal recipe.
- The positive transcript names `bounded_adjacent_pairs_are_exact` and reports
  `VERIFICATION:- SUCCESSFUL`; the negative transcript reports `E0658`.
- `kani --version` from the installed prototype reports
  `Kani Rust Verifier 0.68.0 (standalone)` and `CBMC 6.11.0`.
- Roadmap 1.1.1 is marked done; 1.1.2 and 1.1.3 are released from their hold.

Quality criteria:

- Tests: all unit, property and snapshot tests pass.
- Verification: `INV-identity`, `INV-digest`, `INV-floor`, `INV-runtime`,
  `INV-licence`, `INV-resolution` and `L-parse` are discharged as above.
- Lint/typecheck: Ruff, Pylint, interrogate, `ty`, markdownlint and nixie are
  clean.
- Performance: no runtime threshold applies; the producer performs no I/O.
- Security: no credential, network or publication path is added.

## Idempotence and recovery

The producer code, tests and fixtures are deterministic and safe to re-run.
Building the prototype is repeatable from the same tag; the build directory
lives outside the repository and can be discarded. Recapturing a transcript
overwrites a fixture and must be reviewed as an evidence change. Refreshing the
Syrupy snapshot is only appropriate for an intended recipe change.

## Artefacts and notes

Prototype setup and version:

```plaintext
$ cargo-kani kani setup --use-local-bundle ./kani-0.68.0-x86_64-unknown-linux-gnu.tar.gz \
    --use-local-toolchain ~/.rustup/toolchains/nightly-2026-08-21-x86_64-unknown-linux-gnu
[1/5] Ensuring the existence of: .../kani-home/kani-0.68.0
[2/5] Installing local Kani bundle: ./kani-0.68.0-x86_64-unknown-linux-gnu.tar.gz
[3/5] Installing rust toolchain from path provided: .../nightly-2026-08-21-x86_64-unknown-linux-gnu
[5/5] Successfully completed Kani first-time setup.
$ kani --version
Kani Rust Verifier 0.68.0 (standalone)
CBMC 6.11.0
```

Measured runtime requirement:

```plaintext
kani-driver    max_glibc=GLIBC_2.39   (prototype host, Rocky Linux 10)
kani-compiler  max_glibc=GLIBC_2.34
cbmc and goto-* max_glibc=GLIBC_2.34
kissat         max_glibc=GLIBC_2.34
```

Harness verdict:

```plaintext
Checking harness bounded_adjacent_pairs_are_exact...

VERIFICATION RESULT:
 ** 0 of 409 failed (6 unreachable)

VERIFICATION:- SUCCESSFUL
```

## Interfaces and dependencies

`rust_prover_tools/kani_snapshot/producer/recipe.py` exports
`PINNED_UPSTREAM_COMMIT`, `PINNED_UPSTREAM_TAG`, `pinned_recipe() -> Recipe`,
`recipe_to_json(Recipe) -> bytes` and `recipe_digest(Recipe) -> str`.
`recipe_rules.py` exports `validate_recipe(Recipe) -> None`, which raises
`ProverToolError`. `recipe_parse.py` exports `RECIPE_KEYS` and
`parse_recipe(bytes) -> Recipe`. The package exports nothing else at package
level.

Errors use the existing `rust_prover_tools.errors.ProverToolError`. Shared
validation helpers are reused from `rust_prover_tools.kani_snapshot.validation`
and `policy` rather than duplicated. No dependency is added; Hypothesis, Syrupy
and pytest-bdd are already development dependencies in `pyproject.toml`.

## Revision note

- 2026-11-15: initial plan, written after implementation to record the
  delivered recipe, its evidence and its decisions. No remaining scope changed;
  the follow-on work stays with EP-M4 and EP-M5 in the parent plan.
