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
- 19 November 2026: remediated the second completeness assessment (A2,
  `EP258-2026-11-19-A2`). Present-but-wrong release assets are now diagnosed
  before a missing one is uploaded; publication observation resolves
  `refs/tags/<tag>` instead of trusting `target_commitish`, peels annotated
  tags and refuses an orphan tag; a published release is re-verified against
  its own manifest, checksums, attestation and recorded build inputs; check-run
  evidence is paginated to the reported total, retains head SHAs and URLs, and
  is refused when truncated; the launcher plan carries the design's package,
  extract and locked-install steps; and the workflow reads the bundle from the
  workspace root, proves a clean pinned checkout and packs the installed
  launchers. D3's scheduled rule is reconciled with the pinned-recipe guard in
  the design. The lint gate then found the adapter and the workflow-contract
  module at pylint's 400-line limit, so the adapter's two copies of the `gh`
  failure classification became one helper and the build-job contracts moved to
  `tests/test_kani_workflow_build_contract.py`.
- 19 November 2026: remediated the third completeness assessment (A3,
  `EP258-2026-11-19-A3`). Four blocking findings and two in-scope gaps: the
  producer and consumer now share one `shared_libraries` walk, so the manifest
  records exactly the libraries `toolchain_binding` requires, and the workflow
  no longer passes a hand-built `--library` subset; assembly takes the resolve
  job's independently downloaded identity and refuses a tag, upstream commit or
  builder commit the resolve job did not allocate; `provenance_verified` is now
  decided by the consumer's own `gh attestation verify` policy and this run's
  identity, not by structural checks alone; and a published release that does
  not resolve to a tag ref is refused rather than accepted as a no-op. The
  build now copies each source-state observation into the artefact as it is
  taken, uploads what it has on failure under its own artefact name, and writes
  D5's wall time, disk, cache outcome, payload sizes and source HEADs to the
  run summary. Every downstream job names its prerequisite's result as an
  explicit guard; only a status-check function in a job-level `if` drops the
  implicit `success()`, so the named result states intent rather than relying
  on that rule.
- 19 November 2026: remediated the fourth completeness assessment (A4,
  `EP258-2026-11-19-A4`). The shared-library walk now sorts each directory and
  records every shared object by its resolved path, so two copies of one
  toolchain cannot spell the same library differently according to the order
  the filesystem returned it. The run summary reads the measurements from
  `resources` where the evidence document nests them, and the same filter is
  tested by running it over a real evidence document and over the failure-path
  document. A failing plan command no longer discards its observations: wall
  time, disk high-water mark, the final source state and a minimal
  `resources.json` are written before the original status is re-raised, so the
  failure artefact and the summary carry them while the job still fails. The
  Python wheel workflow now recovers an already-published release as a verified
  no-op — upload and publish write nothing, and the unchanged byte comparison
  turns any disagreement into a collision error — matching D5 and the snapshot
  publisher's own rule.
- 19 November 2026: remediated the fifth completeness assessment (A5,
  `EP258-2026-11-19-A5`). `tests/test_python_release_lifecycle.py` now runs the
  wheel workflow's own steps against a stand-in `gh` that serves and mutates
  one recorded release, so the fresh-tag path, an interrupted draft, a matching
  published no-op and the three collision shapes (missing, extra and changed
  bytes) are shown to make exactly the writes they claim — none, for a no-op
  and for a collision. `jq`, which the run-summary contract test needs, is
  documented as a host tool in the developers' guide rather than left implicit,
  and `make workflow-check` includes the new module.
- 19 November 2026: remediated the review round on `c99e900` (48 threads). The
  `ReleaseApi` port now separates `observe(tag)`, which is a metadata query
  that never claims a verification, from `verify(tag, observed, names)`, whose
  digests are computed from the bytes the release serves; the adapter's `gh`
  failure classification is shared through `gh_transport` with a bounded runner
  protocol, and every call carries a timeout. A draft whose upload was
  interrupted leaves an asset record of zero bytes and no digest, which GitHub
  will not let the retry upload over; the policy now returns
  `DISCARD_PLACEHOLDER` for exactly that record, the observation keeps the
  asset's numeric identifier, and the adapter deletes it by identifier before
  the real file is sent. A record with other non-empty bytes is still a
  conflict, so recovery never clobbers verified bytes and `upload` still never
  clobbers. The port's methods carry NumPy-style contracts, and the recheck
  before the PATCH is documented as narrowing rather than closing the window,
  with the post-write observation as the report of any divergence. The users'
  guide registers its smoke-dispatch example with a `tested-example` marker and
  a shared loader in `tests/documentation_examples.py` reads the shipped text,
  holding the documented command and the Makefile recipe to one contract. Test
  modules gained assertion messages, the entrypoint publication fake moved to
  its own module below pylint's line limit, the release-verification digest
  witness now changes one digest per case, and the generated publication
  machine derives whether a state must conflict instead of accepting any error.
  Four production modules this branch had grown past pylint's 400-line limit
  were split along seams that already existed rather than by raising the limit:
  the `ReleaseApi` port into `release_port`, asset classification into
  `release_assets_policy`, the release-body markers into `release_markers`, and
  the adapter's attestation and download steps into `gh_release_attestation` and
  `gh_release_download`. Their public names are unchanged and the layering
  stays acyclic, so no caller needed a shim. The same applies to the test
  modules: the release API double, the run-summary contract and the Makefile
  contract moved into their own modules, and the Makefile's
  `WORKFLOW_CONTRACT_TESTS` list names the new one so `make workflow-check`
  still runs every contract test it did before.
- 19 November 2026: remediated the seventh completeness assessment (A7,
  `EP258-2026-11-19-A7`) by restoring the two boundaries the approved documents
  state. First, the `collect` job is gone: it held `contents: write`, which the
  plan and D5 reserve for publication. The resolve job now reads the release
  list itself with its read-only token, and it no longer downloads an artefact
  to do it, so its only permission is `contents: read`. The consequence is
  recorded rather than hidden: a draft left by an interrupted run is invisible
  to allocation, which numbers the identity from published tags, and
  publication observes the tag again with the writer token and refuses a draft
  bound to another run or attempt. Second, the producer package is pure again.
  Every job command and every path effect moved to a new peer package,
  `rust_prover_tools.kani_snapshot.orchestrator`: `entrypoint`, `operations`,
  `resolve_operations`, `assembly`, `release_evidence`, `measure_build`,
  `build_evidence`, `inventory_build`, the two collectors and the one atomic
  document writer. The shared asset names stay in `producer/asset_names.py`, so
  both layers read one definition without the pure package importing an effect.
  `gh_release_api`, `gh_release_document`, `gh_release_attestation` and
  `gh_release_download` became one `gh_release` package with the same split.
  The workflow invokes only `orchestrator` commands, and
  `tests/test_kani_snapshot_producer_purity.py` reads every producer syntax
  tree and refuses an effectful import, an effectful call or an `orchestrator`
  import. The condition tests were rewritten for the simpler graph: no job
  accepts a skipped prerequisite, so every condition is an ordinary comparison
  and the implicit `success()` check is what stops a job after a failed
  prerequisite.
- 19 November 2026: remediated the eighth completeness assessment (A8,
  `EP258-2026-11-19-A8`). Its one blocking finding was a remaining ordering
  hazard in draft recovery: a present asset whose digest the API did not report
  was classified as unverified, but the policy deleted a placeholder or
  uploaded a missing file before anything downloaded and hashed it. A same-size
  asset with different bytes was therefore discovered only after the draft had
  been edited, which is the failure mode the policy's own ordering rule
  forbids. The policy now hashes every present asset with no observed digest
  before an edit, through a new `HASH_ASSETS` step and a `hash_assets` port
  method that downloads only the named assets and claims no provenance. A
  conflicting draft is refused with no discard, upload or publish, and a draft
  whose bytes match still recovers. A draft that needs no edit skips the extra
  download and is verified whole instead, because that verification already
  hashes every asset. The generated state machine now runs a second history
  with no volunteered digests, so the pre-edit byte check is exercised over
  generated transitions. The developers guide and D5 record the split between a
  byte check and a provenance verdict, and the developers guide now names
  `download_unverified` as the one bounded exception to the `download_pinned`
  rule instead of implying that no download path may use anything else.
- 19 November 2026: remediated the ninth completeness assessment (A9,
  `EP258-2026-11-19-A9`). Three findings were open. First, publication was
  decided from the asset listing the recheck made before its download, so an
  asset removed after its own bytes were hashed was still published and the
  loss surfaced only once the release was public. `publish._recheck` now reads
  the draft again once verification completes: when the two readings agree it
  decides on the verified observation, and when they disagree it decides on the
  newer reading, which restores the vanished asset before anything becomes
  public. `tests/test_kani_snapshot_publish_race.py` fails on the old code with
  a publish that precedes the upload. Second, the `verify` port contract
  claimed a result could never describe another release than the observation,
  which the adapter cannot prove: it downloads by tag, and a tag is not a
  lease. The docstring now states that limit and says the caller confirms the
  state after the download, as publication does. Third, the smoke example in
  the users guide is executed rather than string-matched:
  `tests/test_workflow_makefile_contract.py` loads the fence from the guide,
  substitutes representative inputs and runs it against the `gh` stand-in,
  asserting the exact dispatch vector including `publish=false`.
- 20 November 2026: remediated the tenth completeness assessment (A10,
  `EP258-2026-11-20-A10`). Its blocking finding was that the recheck's
  comparison covered only the asset set, so a draft retargeted at another
  commit or re-bodied to record another attempt while its bytes were being
  downloaded was still published from the pre-download identity the policy had
  approved. `_state_key` now includes every field the policy reads — tag,
  target commit and body, as well as the release id, draft flag and each
  asset's name, size and server identifier. Two cases in
  `tests/test_kani_snapshot_publish_race.py` change the target and the body on
  the second verification and assert a conflict with no publish call; both
  publish on the previous code. D5, the developers guide and the users guide
  say the confirming reading covers identity as well as assets.

- 20 November 2026: remediated CodeRabbit's review of `b5cbf34` (18 inline
  findings). The confirming read A9 introduced for publication now covers every
  download whose verdict authorizes a later step. `publish._settle` reads the
  release once more after a `HASH_ASSETS` or verification download and discards
  the completed reading when the state moved, so an asset replaced mid-hash no
  longer authorizes an edit and a published release re-bodied during
  verification is no longer reported up to date. A published release whose
  recorded inputs or asset names already refuse it is decided from the
  metadata, before a download is requested, so a transport failure cannot
  displace the conflict. The three repairs have named tests in
  `test_kani_snapshot_publish_hash`, `test_kani_snapshot_publish` and
  `test_kani_snapshot_release_policy`. Three external-data boundaries were
  tightened: `gh release download --pattern` reporting that nothing matched is
  now a transient failure (pinned by message), a check run that omits
  `started_at` is refused instead of being projected as a queued run, and the
  run marker's digit count is capped so an absurd body is refused rather than
  raising from `int()`. Inventory building accepts the archive's own hard-link
  carriers through `--archive`/`--archive-root`, verifies the claimed carrier
  shares the member's inode, and reads link text with `os.readlink` so a leading
  `./` survives to match the tarball header; the workflow passes both flags.
  `write_document` puts directory creation and the temporary file inside its
  error boundary and removes a half-created temporary file. The
  documentation-example loader now refuses an unmarked fence, a marker with no
  identifier and a relative-package import, the producer-purity check records
  `from pkg import submodule` by its dotted name, the migration guide's
  snapshot commands are registered and executed against the CLI, and the
  Makefile contract tests sandbox `HOME` so a competing `gh` cannot be reached.

- 20 November 2026: closed the two pre-merge rows that review left open, and
  kept both edited modules inside pylint's size limit. A direct test of
  `download_unverified` now covers reuse of a file that fits the ceiling
  without a network request, replacement of an oversized file, and a first
  download; the developers' guide names every new internal boundary — the
  producer's job documents, identity, fingerprint, evidence and receipt
  modules, the publication modules including `publish_request`, `release_model`
  and `gh_transport`, and the orchestrator's `operations`, `resolve_operations`,
  `release_evidence` and `inventory_carriers`. The request precondition moved
  to `publish_request` and the archive-carrier checks to `inventory_carriers`,
  leaving `publish` and `inventory_build` below the 400-line convention message
  without changing behaviour; the existing publication and inventory tests pass
  unchanged.

- 20 November 2026: remediated the second review round on `a2840bf`, which
  returned two inline findings and four follow-ups to answers it had given
  earlier. The inventory build now receives a workspace-absolute `--archive`:
  `uv --directory` runs the child from `builder`, while the bundle is copied to
  `$GITHUB_WORKSPACE/payloads`, so the relative form named a file that never
  existed and the job failed before writing `inventory.json`. The release
  double records an asset identity when it places bytes, so the replacement
  test now proves a changed identity instead of an id merely appearing. The
  documentation-example loader recognizes `~~~` fences and closes each fence
  only with its own character, closing the last way an unregistered example
  could hide. The documented-example runner pins `UV=uv`, because an inherited
  absolute `UV` puts its directory first on `PATH` and a `gh` beside it would
  dispatch the hosted workflow with the caller's credentials. The
  missing-download witness is renamed for what it does — the asset is gone
  before the download, which the adapter reports as a transient failure — and
  the test says so. A new round-trip test,
  `test_kani_snapshot_inventory_archive`, builds an archive whose carrier sorts
  after the link that names it, generates the inventory through `--archive` and
  `--archive-root`, and passes it to `validate_archive_inventory`; a walk alone
  records the opposite carrier, so the test binds the archive parsing rather
  than a supplied carrier map.
- 20 November 2026: remediated the third review round on `7b891cd`, whose
  Domain Architecture and Testing rows returned to the converge loop and named
  three further boundaries. A budget that ends on the publish call is now
  followed by one confirming observation, and reports an unconfirmed
  publication instead of "did not converge": the write may have landed, so the
  failure may not claim nothing happened. Four tests hold it, including a lost
  publish response on the last step that must not be sent twice, an unreadable
  confirmation, and a budget of exactly the six steps the transition needs,
  which fails if the loop bound spends a step on itself. The adoption refusal
  and the unresolved-tag refusal moved out of the `gh` adapter into
  `release_policy`, driven by a new `ObservedRelease.tag_commit`, so the
  adapter only translates. The local asset precondition moved from
  `publish_request` into `publish`, leaving the request a pure record.
  `publish_observation` took the reading-comparison helpers so `publish` stays
  below pylint's module budget. The documented example runner drops inherited
  `MAKEFLAGS`, `GNUMAKEFLAGS` and `MAKEOVERRIDES`, because a parent make exports
  `UV=/absolute/uv` through them and that outranks the pin, reopening the
  decoy-`gh` edge; the decoy regression now carries that channel.
- 20 November 2026: split two test modules that the round above pushed past
  pylint's 400-line module budget, rather than trimming the cases that crossed
  it. The step-budget witnesses moved to
  `tests/test_kani_snapshot_publish_budget.py`, and the published-release
  witnesses to `tests/test_kani_snapshot_release_policy_published.py`, which
  leaves each module naming one facet of the state machine. The absent-state
  helper now takes the tag ref it already addresses, so the adoption case reads
  as one observation.

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
- `cmd_mox` keeps one double per command name, so two `.mock("gh")`
  registrations in one test compete rather than compose. A test needing a
  release document and a tag ref answers both from one stub handler.
- Upstream `cargo bundle` runs `build-kani` from the workspace root, so the
  tarball lands in `upstream/` rather than `upstream/target`; the assemble step
  reads it there and fails if it is absent.
- `cargo package` writes `<crate>.crate` beside the unpacked directory it
  verifies, so the plan extracts the archive beside it and installs from the
  unpacked path into a private prefix.
- pylint's `too-many-lines` counts physical lines and its 400-line limit is
  reached by a feature-sized module, so a split is cheaper than a suppression.
  The build-job contracts now have their own module, and the adapter's three
  copies of the `gh` failure classification became one `_failure` helper.
- A job-level `if:` in GitHub Actions replaces the implicit `success()` check
  on `needs` only when the condition contains a status-check function such as
  `always()`, `failure()` or `cancelled()`; an ordinary comparison keeps that
  check, so a failed prerequisite already prevents the job from running. Every
  guarded job still names `needs.<job>.result` explicitly, as an additional
  guard that states which prerequisite outcomes the job accepts, so a job
  deliberately allowed to run after a failure cannot rely on the implicit rule
  changing.
- The producer's measurement and the consumer's coverage check were two
  definitions of "the toolchain's shared libraries": one trusted a `--library`
  list assembled in YAML, the other walked `<toolchain>/lib`. They now call the
  same `shared_libraries` function, and a test pins the measured set equal to
  the consumer's walk.
- `gh attestation verify` is usable from the adapter as well as from a
  consumer: it reports a non-zero exit for a bundle outside the pinned policy
  or for another run's attempt, which is exactly the signal
  `provenance_verified` needs.
- GitHub lists draft releases only to a push-capable token, so removing the
  write-capable collection job also removes draft visibility from allocation.
  The allocator numbers an identity from published tags, and publication —
  which does hold the writer token — re-observes the tag and refuses a draft
  bound to another run or attempt. Recovery is therefore a refusal with a
  reason, not a resumed draft; the earlier design that resumed one is only
  available to a run that can still see its own draft.

## Decision log

- *One module owns the shared-library set.* `shared_libraries` walks
  `<toolchain>/lib`, refuses a directory or link that escapes it, and is the
  only definition of the set. `measure_build` hashes what it returns;
  `toolchain_binding` refuses any unrecorded file it returns.
- *Assembly takes the identity from the job that allocated it.* The build
  artefact is produced by the least trusted job, so the tag, upstream commit
  and builder commit it reports are compared with the resolve job's own
  identity, downloaded separately. Publication's target therefore cannot be
  chosen by the build.
- *`provenance_verified` includes the signature.* A published release's
  attestation is checked with the consumer's `AttestationPolicy` — this
  repository's signer workflow, the manifest's builder commit as source and
  signer digest — and the verified statement must name this run and attempt.
  The structural checks stay as a cheap refusal and never decide the flag.
- *A published release must resolve to a tag.* A published release whose tag ref
  cannot be resolved has no confirmed target, so observation refuses it instead
  of letting policy reach a no-op.
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
  attestation bundle. Its receipt is a record of those checks, not a statement
  about the signature; the publisher's own observation verifies the signature
  under the consumer's policy, and EP-M5's clean runner exercises it end to end.
- *The Python release is draft-first.* Creating a public release before
  uploading publishes immutable assets that cannot be repaired. The workflow
  now creates or resumes a draft, uploads only missing wheels, downloads and
  byte-compares the release's assets, and publishes last.
- *A published Python release is a verified no-op, not a refusal.* D5's rule
  applies to both publishers: an existing published release is accepted only
  when it serves exactly the expected asset names and bytes, and any
  disagreement is a collision. The wheel workflow records the published state,
  skips upload and publish, and still runs the ordinary byte comparison, so
  recovery never writes while a collision still fails.
- *Observations survive a failed build.* The plan command's exit status is
  captured rather than allowed to end the step, the wall time, disk high-water
  mark and final source state are written to the artefact, and the status is
  then re-raised. A failed run therefore explains itself without becoming
  publishable.
- *The shared-library walk is canonical and ordered.* Entries are sorted and
  each shared object is recorded by its resolved path inside the toolchain.
  `os.scandir` order is arbitrary and aliased directories would otherwise spell
  one library two ways, which would make the manifest disagree with the
  consumer's own walk.
- *Workflow checks are a Makefile target and a CI step.* `make workflow-check`
  runs the contract tests and actionlint; CI installs a digest-verified
  actionlint release and runs the target. `make all` includes it,
  `kani-snapshot-smoke` deliberately does not.
- *The scheduled route selects but does not self-advance.* D3 says to build the
  newest qualified release; the recipe is reviewed data, so the route selects
  that release and refuses it unless it is the pinned one, naming the recipe
  update required. The design records this narrowing explicitly rather than
  leaving the code and the approved text disagreeing.
- *The adapter, not release metadata, decides the commit.* GitHub ignores
  `target_commitish` when the tag exists, so observation resolves the tag ref,
  peels annotated tags and refuses an orphan tag before any write. This keeps
  the policy's existing commit comparison meaningful for drafts and published
  releases alike.
- *The producer is pure and the orchestrator holds every effect.* The approved
  plan's producer line is read literally: no path read or write, no process, no
  environment and no network call in `kani_snapshot.producer`. Job commands and
  file effects live in `kani_snapshot.orchestrator`, which imports the producer
  and is never imported by it. Asset names shared by both layers live in
  `producer/asset_names.py`, so the split costs one module rather than a
  duplicated constant. A structural test enforces the direction.
- *Only publication holds `contents: write`.* The resolve job reads what it
  needs with `contents: read`, including the release list. Because a read token
  cannot see drafts, draft recovery belongs to publication, which re-observes
  the tag before it writes and refuses a foreign draft instead of adopting it.
- *`provenance_verified` states exactly what the publisher checked.* A published
  release must re-verify from its own bytes: manifest, checksums, payloads, a
  well-formed SLSA statement naming the manifest digest, identities that
  reproduce the body's build-input fingerprint, and — since A3 — a signature
  the consumer's own policy accepts for this run and attempt. The structural
  checks run first as a cheap refusal; the verifier alone decides the flag.
- *A draft is hashed before it is edited, and verified only when it needs no
  edit.* An absent API digest means nobody has hashed the bytes, not that the
  bytes are sound. The policy therefore downloads and hashes every present
  asset with no observed digest before it deletes a placeholder or uploads a
  missing file, through `hash_assets`, which downloads only those names and
  claims no provenance. A draft that needs no edit is verified whole instead,
  because that download already hashes every asset; paying for both would
  double the transfer for no extra evidence.
- *Publication is decided from a reading taken after its download.* The listing
  a recheck starts from cannot show an asset removed while its bytes were being
  hashed, so the recheck reads the draft again and acts on the newer reading
  when the two disagree. The comparison covers every field the policy reads —
  tag, target commit, body and asset identities — not just the asset set,
  because a retargeted or re-bodied draft is one the policy would refuse. The
  window between that reading and the PATCH is unavoidable — GitHub's release
  update has no conditional form — and the observation after the write is what
  reports a divergence there.

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

- `orchestrator/operations.py` — the five subcommands' orchestration, each
  checking input and same-run binding before any effect.
- `orchestrator/entrypoint.py` — argument parsing and dispatch only.
- `orchestrator/collect_candidates.py`, `orchestrator/collect_releases.py` —
  bounded GitHub reads, projected by the producer's pure modules.
- `orchestrator/measure_build.py` — build-evidence measurement from observed
  files.
- `orchestrator/build_evidence.py`, `orchestrator/inventory_build.py`,
  `orchestrator/assembly.py`, `orchestrator/release_evidence.py`,
  `orchestrator/documents.py` — the file steps and the one atomic writer.
- The pure producer keeps the records and rules those steps call:
  `producer/publication_identity.py`, `producer/assemble_input.py`,
  `producer/lifecycle.py`, `producer/lifecycle_model.py`,
  `producer/validation_receipt.py`, `producer/asset_names.py`.

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
- `tests/test_kani_workflow_contract.py` — the snapshot workflow contracts for
  the guard, permissions, budgets and artefact routing.
- `tests/test_kani_workflow_build_contract.py` — the build job's hand-offs:
  pinned solver digests, bundle location, source-state evidence and the
  launcher archive.
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

- `python -m rust_prover_tools.kani_snapshot.orchestrator.entrypoint {resolve,identity,build,assemble,validate,publish}`
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
