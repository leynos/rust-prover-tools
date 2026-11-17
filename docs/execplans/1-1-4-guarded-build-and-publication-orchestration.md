# Deliver guarded build and publication orchestration

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE (implementation and gates; hosted evidence remains EP-M5)

Draft pull request: <https://github.com/leynos/rust-prover-tools/pull/258>

Branch: `1-1-4-guarded-build-and-publication-orchestration`

Roadmap item: `1.1.4` in `docs/roadmap.md`. Parent milestone EP-M4 in
`docs/execplans/kani-monthly-snapshot-distribution.md`.

## Purpose / big picture

Roadmap items 1.1.1 to 1.1.3 delivered a pinned recipe, validated pins and
authenticated manifests, and an isolated consumer installer. Nothing yet
produced a snapshot automatically, and the repository's ordinary Python release
workflow made a public release before uploading its wheel, which published
assets can never repair.

After this work, `.github/workflows/kani-monthly.yml` builds, attests,
validates and publishes one immutable Kani snapshot per month, behind guards
that refuse any repository, ref or event that is not the trusted one. Every
decision the workflow could get wrong lives in a tested module under
`rust_prover_tools/kani_snapshot/producer`, and the workflow's shell only
composes observed documents and executes the pinned plan. Scheduled runs stay
disabled until an operator sets `KANI_SNAPSHOTS_ENABLED`, so merging this work
changes no live behaviour. `.github/workflows/release.yml` now publishes the
Python wheel draft-first and verifies the release's bytes before making it
public.

Nothing is published by this work. EP-M5 (real non-publishing smoke evidence
and the operations runbook) and EP-O1 (operational bootstrap) remain open in
the parent plan.

Terms used here:

- *resolution*: choosing the upstream commit to build, from scheduled evidence
  or from a dispatched full SHA.
- *identity*: the immutable publication tag
  `kani-snapshot-YYYY-MM-<12 hex>-rN`, and the builder commit and inputs it
  names.
- *assembly*: measuring payloads and writing `manifest.json` and `SHA256SUMS`.
- *receipt*: the validation evidence that authorizes publication for one run
  and attempt.
- *draft recovery*: resuming an interrupted draft with the same run and
  attempt, uploading only missing assets, and never replacing bytes.

## Constraints

- The workflow must not publish, sign or allocate an identity unless the run is
  the canonical repository, `refs/heads/main`, and a schedule or dispatch.
- Top-level permissions are empty. Only assembly receives `id-token: write` and
  `attestations: write`; only publication receives `contents: write`.
- Upstream source and build output are untrusted data. Attestation and
  publication jobs must never execute anything from them.
- Artefacts are retrieved by exact same-run identifiers, never by an arbitrary
  name from another run.
- Published tags and assets are immutable. Recovery is a code revert or a
  corrected new identity, never asset replacement.
- The Python release keeps its `v*.*.*` namespace, wheel contents and generated
  release notes.
- The producer package stays pure: no subprocess, network, environment or file
  system effect. Adapters receive its argument vectors.
- Ordinary `make test` performs no real build.
- Every gate in `AGENTS.md` passes before a commit: `make check-fmt`,
  `make typecheck`, `make lint`, `make test`, and for documentation
  `make markdownlint` and `make nixie`.

## Tolerances (exception triggers)

- If actionlint cannot lint a file the contract tests read, stop and record why
  rather than dropping the contract.
- If a contract test would need to assert a specific Action SHA value, assert
  only the 40-hex shape instead; Dependabot owns the value.
- If a check cannot actually be performed (for example cryptographic
  attestation verification in the integrity job), record it as a presence check
  and leave the verification to its real owner. Do not let a receipt claim a
  check nobody ran.
- If a host lacks actionlint, `make workflow-check` fails with an actionable
  message and `ACTIONLINT` can be overridden; it does not silently skip.

## Risks

- A workflow contract test can drift from what GitHub actually runs. Mitigated
  by a strict loader that refuses duplicate keys and keeps `on:` a string.
- The build job is the least trusted. Mitigated by moving every interpretation
  of its observations into tested modules and by failing when a solver binary
  or library is absent rather than recording a gap.
- A rerun increments the attempt. Mitigated by naming artefacts with both
  `github.run_id` and `github.run_attempt` and by refusing a receipt from
  another attempt.
- Draft-first publication changes a release path users depend on. Mitigated by
  keeping the wheel, interpreter, artefact name and notes generation identical
  and pinning all of it in `tests/test_python_release_contract.py`.

## Progress

- 17 November 2026: release state machine, policy and `gh` adapter implemented
  with hypothesis stateful coverage and cmd-mox failure injection (`c30c569`).
- 17 November 2026: producer entrypoint, assembly, lifecycle and receipt
  implemented with syrupy snapshots (`0a574bf`).
- 17 November 2026: build evidence, inventory, publication identity and release
  assets implemented (`30105ff`).
- 17 November 2026: scheduled candidate collection (`1281d7e`), build-evidence
  measurement (`7673f22`) and existing-release collection (`c7db1f1`).
- 17 November 2026: manual dispatch no longer supplies an upstream tag; the
  pinned recipe owns it (`d16cd3d`).
- 17 November 2026: `.github/workflows/kani-monthly.yml`, its strict contract
  loader and 20 contract tests added; `make workflow-check` and
  `make kani-snapshot-smoke` added; CI installs pinned actionlint and runs the
  check (`ecfd292`).
- 17 November 2026: Python release workflow rewritten draft-first, with the
  composite action and workflow Actions pinned and 14 contract tests
  (`4dab020`).
- 17 November 2026: design, users' and developers' guides updated (`e8db01e`).
- 17 November 2026: full commit gates run sequentially. Formatting,
  typechecking, lint (interrogate 100%, pylint 10.00/10), 1760 tests (5
  pre-existing environment-gated skips), CV-005 workflow contracts,
  `workflow-check` (69 contract tests plus actionlint), Markdown lint and
  Mermaid validation all passed. The spelling gate caught one en-GB-oxendict
  spelling, `authorises`, which is fixed to `authorizes`.
- 17 November 2026: draft pull request #258 opened; roadmap 1.1.4 marked done.

## Surprises & discoveries

- PyYAML parses `on:` as the boolean `true` under YAML 1.1, so a contract test
  reading a workflow with the default loader asserts something GitHub never
  runs. Adding an implicit resolver is not enough: the inherited boolean
  resolver is consulted first, so the strict loader rebuilds the resolver table
  without the bool tag before adding a `true`/`false`-only resolver.
- PyYAML also keeps the last duplicate key silently. Both problems are fixed
  once, in `tests/workflow_contracts/strict_workflow.py`.
- actionlint cannot lint composite action files, so
  `.github/actions/pure-python-wheel/action.yml` is covered by a contract test
  instead.
- The attestation action exposes only a `bundle-path` output; recording
  `manifest.sigstore.jsonl` is a separate copy step.
- `uv --directory` changes the child working directory, so every path passed to
  a `uv --directory` invocation is written as an absolute `$GITHUB_WORKSPACE`
  path.
- A dispatch that supplies only a commit cannot also supply a trusted tag
  without letting the two disagree. The tag now defaults to the pinned
  recipe's, and a foreign commit still fails at plan time.

## Decision log

- *Build evidence is measured in Python, not shell.* The build job observes
  files: compiler identities, channel manifest, lock digests, solver digests
  and versions, wall time and disk high-water mark. `measure_build` turns them
  into a typed document so schema drift fails loudly instead of silently
  producing a manifest with a gap.
- *Release-list and candidate projections are modules.* `collect_releases` and
  `collect_candidates` project bounded GitHub responses. A shell step may
  compose documents; it may not decide what a response means.
- *Manual dispatch names a commit, not a tag.* `operations._manual_resolution`
  takes the tag from `pinned_recipe()`. This keeps the three documented
  dispatch inputs and removes any way to pair a foreign commit with a trusted
  tag.
- *Validation records what it verifies.* `validate` checks payload digests, the
  manifest bytes on disk, the exact `SHA256SUMS` rows and a non-empty
  attestation bundle. Cryptographic verification stays with the consumer
  verifier and with EP-M5's clean runner; the design document and developers'
  guide say so explicitly.
- *The Python release is draft-first.* Creating a public release before
  uploading publishes immutable assets that cannot be repaired. The workflow
  now creates or resumes a draft, uploads only missing wheels, downloads and
  byte-compares the release's assets, and publishes last.
- *Workflow checks are a Makefile target and a CI step.* `make workflow-check`
  runs the contract tests and actionlint; CI installs a digest-verified
  actionlint release and runs the target. `make all` includes it,
  `kani-snapshot-smoke` deliberately does not.

## Outcomes & retrospective

The EP-M4 plateau is met: a default-disabled scheduled capability, a manual
validation mode and a publication adapter that pass deterministic gates, with
repository settings unchanged. Conformance covers U4 permissions, authenticated
inputs, lifecycle and the Python namespace. Recovery is a code revert or a
corrected new publication identity, never asset replacement.

What went well:

- Moving every projection of external data into a tested module kept the
  workflow's shell small enough to review, and it made the strict contract
  reader possible: the tests can assert intent rather than YAML trivia.
- Writing the contract tests before the workflow caught two real defects: a
  workflow path resolved relative to a changed working directory, and a manual
  dispatch that could not supply the upstream tag.

What to carry forward:

- A check named in a receipt must be a check that ran. The `attestation` check
  is deliberately a presence check, and both the design document and the
  developers' guide say so; do not let a later change quietly relabel it.
- actionlint cannot read composite actions, so any new composite action needs
  its own contract test.
- EP-M5 must exercise the real signature policy on a clean runner before the
  schedule is enabled.

## Context and orientation

Producer modules added or extended on this branch:

- `producer/operations.py` — the five subcommands' orchestration, each checking
  input and same-run binding before any effect.
- `producer/entrypoint.py` — argument parsing and dispatch only.
- `producer/collect_candidates.py`, `producer/collect_releases.py` — bounded
  GitHub response projections.
- `producer/measure_build.py` — build-evidence measurement from observed files.
- `producer/build_evidence.py`, `producer/inventory_build.py`,
  `producer/publication_identity.py`, `producer/assemble.py`,
  `producer/assemble_input.py`, `producer/lifecycle.py`,
  `producer/lifecycle_model.py`, `producer/validation_receipt.py`,
  `producer/release_assets.py`.

Consumer-side publication modules added on this branch:

- `kani_snapshot/release_model.py` — frozen release state and asset records.
- `kani_snapshot/release_policy.py` — the pure transition policy.
- `kani_snapshot/publish.py` — orchestration to a recorded no-op.
- `kani_snapshot/gh_release_api.py` — the `gh` adapter.

Workflow and test surfaces:

- `.github/workflows/kani-monthly.yml` — guard, resolve, build, assemble,
  validate, publish.
- `.github/workflows/release.yml` — draft-first Python release.
- `tests/workflow_contracts/strict_workflow.py` — strict workflow reader.
- `tests/test_kani_workflow_contract.py` — 20 snapshot workflow contracts.
- `tests/test_python_release_contract.py` — 14 release lifecycle contracts.

## Conformance basis

- **V6 (build provenance is bounded and attributable).** The resolution route
  is bounded and fails when its list is exhausted; build evidence records
  re-executed identities and digests rather than text supplied by the build;
  assembly measures payloads itself.
- **V7 (only complete validated releases become public).** The pure release
  policy never returns a publish action while an asset is absent, mismatched or
  unverified. `tests/test_kani_snapshot_publish.py`,
  `tests/test_kani_snapshot_publish_machine.py` (hypothesis state machine with
  up to 30 generated operations), `tests/test_kani_snapshot_release_policy.py`
  and `tests/test_kani_gh_release_api.py` (cmd-mox failure injection) cover the
  transition model. A seeded publish-before-upload and a cross-run receipt both
  fail. The workflow contract tests pin the trigger namespace, scheduled
  disable default, dispatch inputs, permissions, pin shape, artefact routing,
  budgets and job order.
- **U4 (trusted automation only).** Every job's guard, permission set and
  checkout is asserted; assembly is the only signing job and publication the
  only writer; no job runs anything from the upstream checkout except the
  unprivileged build.
- **Python namespace stability.** `tests/test_python_release_contract.py`
  asserts the `v*.*.*` trigger, the unchanged composite action, interpreter and
  artefact name.

## Verification plan

```sh
make check-fmt
make typecheck
make lint
make test
make spelling
make test-workflow-contracts
make workflow-check
make markdownlint
make nixie
```

The focused publication and workflow contracts:

```sh
python3 scripts/uv_gate.py run -- pytest -q \
  tests/test_kani_snapshot_publish.py \
  tests/test_kani_snapshot_publish_machine.py \
  tests/test_kani_snapshot_release_policy.py \
  tests/test_kani_gh_release_api.py \
  tests/test_kani_workflow_contract.py \
  tests/test_python_release_contract.py
```

`make kani-snapshot-smoke` and any real hosted run are EP-M5 evidence, not gate
steps.

## Plan of work

1. Release state machine, policy and adapter, with stateful and failure
   injection tests. Done.
2. Producer entrypoint, assembly, lifecycle and receipt. Done.
3. Build evidence, inventory, identity and release-asset derivation. Done.
4. Scheduled evidence collection and build-evidence measurement. Done.
5. Workflow, strict contract reader, contract tests, Makefile and CI target.
   Done.
6. Python release lifecycle fix and its contract tests. Done.
7. Documentation, roadmap entry and review. Done; review continues on the
   draft pull request.

## Concrete steps

1. Run the full gateway set with
   `env -u FORCE_COLOR make <target> 2>&1 | tee /tmp/<target>-rust-prover-tools-1-1-4-guarded-build-and-publication-orchestration.out`.
2. Fix any failure in the cited log and re-run the affected gate only.
3. Push the branch over SSH, refresh the tracking ref, and open a draft pull
   request titled `(1.1.4) Deliver guarded build and publication orchestration`
   linking this plan.
4. Mark roadmap 1.1.4 done once the implementation is complete and reviewed.

## Validation and acceptance

Acceptance is the EP-M4 plateau: a default-disabled scheduled capability, a
manual validation mode and a publication adapter that pass deterministic gates,
with repository settings unchanged. Conformance covers U4 permissions,
authenticated inputs, lifecycle and the Python namespace. Recovery is a code
revert or a corrected new publication identity, never asset replacement. Real
smoke and hosted evidence remain EP-M5.

## Idempotence and recovery

Rerunning `make workflow-check` and the test suite is idempotent. Publication
is idempotent by construction: a matching published release is a recorded
no-op, and an interrupted draft is resumed by the same run and attempt. If a
gate fails on a stale cache, remove only that gate's outputs rather than the
shared Cargo or uv cache.

## Artefacts and notes

- The workflow's job budgets are 10 minutes for resolution, 180 for build, 15
  for assembly, 45 for validation and 15 for publication.
- Every Action reference is pinned to a full 40-hex commit, verified as a commit
  object through the GitHub API when pinned.
- The contract tests assert pin shape, never a specific SHA value.

## Interfaces and dependencies

- `python -m rust_prover_tools.kani_snapshot.producer.entrypoint {resolve,identity,build,assemble,validate,publish}`
  is the internal automation interface. It is not registered on the consumer
  CLI.
- Publication depends on `gh` on `PATH`; validation depends only on the
  documents and payload bytes.
- `make workflow-check` depends on actionlint and the pytest environment;
  `ACTIONLINT` is overridable.

## Revision note

Initial version, written after implementation and before the full gate run and
pull request. Revised after the gates passed and pull request #258 opened: the
status, progress entries and outcomes now record the run rather than the plan.
