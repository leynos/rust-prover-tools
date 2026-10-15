# Add monthly Kani snapshot distributions

Status: DRAFT — awaiting explicit approval before implementation. Date:
2026-09-27. Design revision: 3.

This execution plan is a living document. Keep its constraints, tolerances,
risks, progress, discoveries, decisions, verification evidence and outcomes
current at each milestone. This invocation delivers only reviewed planning
Markdown and a draft planning pull request (PR). Approval of that plan is
required before any code, prototype, build, workflow dispatch or publication.

## Purpose / big picture

A consumer will select one reviewed immutable Kani snapshot, install its
prebuilt launcher and upstream-format backend without compiling Kani or any
solver, inspect the exact selected compiler and build identity, and run proofs
through that installation. Initially only `x86_64-unknown-linux-gnu` is
supported. The exact prebuilt Rust toolchain may be downloaded during setup;
compiling a proof fixture is expected. A monthly producer builds centrally,
validates in a clean environment and can publish immutable pre-release assets.

Observable implementation success is a guarded workflow plus deterministic
failure tests and a non-publishing clean-install/proof smoke build. Operational
success is a separately authorized first public release, independently
installed from public assets, followed by schedule enablement. Neither a
planning PR nor an implementation merge counts as that first publication.

## Constraints

Work only in `leynos/rust-prover-tools`. Preserve `tools/kani/VERSION`, legacy
release installation and existing CLI options, output and environment
behaviour. Keep the package Python-only; its actual minimum is Python 3.14. Do
not add a Rust extension, commit upstream source checkouts or generated
binaries, modify Axinite, implement its proof crate, or migrate consumers.
Defer other targets, containers, source-change watchers and automatic upgrades.
Passing repository-owned fixtures cannot establish full-crate compatibility.

All external commands pass through `rust_prover_tools.commands` and Cuprum.
Keep Cyclopts handlers thin, options typed, validation pure, and feature
boundaries independent of Verus assumptions. Do not silently change upstream
Cargo locks, toolchain pins or version metadata. No source-install fallback,
mutable consumer selector, source fork without approval, release clobbering,
tag movement or deletion of historical snapshots is permitted.

No live GitHub setting change or actual publication belongs to implementation
acceptance. Operational bootstrap remains separately authorized. The planning
branch is `kani-monthly-snapshot-distribution` and must track
`origin/kani-monthly-snapshot-distribution` after its first ordinary push.
Preserve existing work; never force-push or overwrite another branch.

Use repository Makefile targets, sequential gates, `/tmp` for logs only, and
the shared default Cargo cache. Do not kill other agents' processes. Stop and
report full disks. Each implementation change follows tests first and is
committed only after its relevant gates pass. No source tests are necessary for
this planning-only Markdown change; document gates and expert review are its
evidence.

## Tolerances (exception triggers)

Stop implementation, record a proposed deviation with affected D/EP
identifiers, set status `BLOCKED`, and obtain explicit approval if any hard
constraint would change; a new runtime Python dependency or unsupported
upstream patch is needed; a public command/schema/trust policy differs from
D1–D5; a candidate cannot meet Rust 1.94; redistribution terms cannot be met;
or ordinary PRs would need real builds. Do not substitute a newer nightly to
make a candidate qualify.

Feasibility permits at most one cold full upstream build plus one cache-repeat
build across EP-M1 and EP-M5 before revisiting a failed recipe. Reserve the
cache-repeat for the trusted EP-M5 run; reuse EP-M1 measurements and cached
dependencies, but never relabel prototype assets as same-run hosted evidence.
Any necessary additional full build requires a recorded budget decision.
Repeated unexplained failure after three focused repair attempts requires
diagnosis review rather than weakening gates. A larger paid runner, additional
platform, job timeout beyond D5, archive limits beyond D4, or disk usage beyond
available runner capacity requires a budget/design decision. There is no
arbitrary developer wall-clock deadline. Reaching a tolerance is not permission
to proceed around it.

Within those bounds, implement milestone by milestone without additional
permission requests. At every boundary check assigned requirements, legacy
compatibility, upstream assumptions, dependency/trust/schema changes and
acceptance links; record the result before advancing.

## Risks

High impact: upstream setup may create absolute Rustup symlinks or identify
installations by package version. EP-M1 inspects and exercises that interface;
EP-M3 uses permanent generation directories and per-identity homes, never a
post-setup directory move. Likelihood remains unmeasured until the experiment.

High impact: upstream compiler pins may be below 1.94 or a candidate may lack
successful Linux checks. Resolve a full SHA and verify actual compiler and
named proof harnesses; defer a candidate rather than change its nightly. Source
inspection alone is not a successful smoke test.

High impact: archive links, redirects, cache corruption or privileged execution
could break the trust boundary. Authenticate exact manifest bytes before
payload extraction/execution, validate complete member graphs, and isolate
build, attestation, validation and publication jobs. Property tests complement
named attacks and real filesystem/process boundary tests.

Medium impact: hosted runner disk/time, runtime libraries, component licences,
attestation CLI flags and offline trust roots may prevent binary-only setup.
EP-M1 records actual requirements and a bounded feasibility verdict. No cost or
bit-for-bit reproducibility claim is made without measurements.

Medium impact: interrupted releases become unusable under repository-wide
immutability. EP-M4 fixes the existing wheel release's upload ordering before
bootstrap may enable that setting, and tests retries at every transition.

Medium impact: GitHub monthly schedules can be delayed, dropped or disabled
through inactivity. EP-M5 supplies monitoring and manual recovery instructions;
EP-O1 assigns responsibility and records live evidence.

## Progress

- [x] (2026-09-27) Read repository guidance, source, tests and release baseline;
  no existing roadmap was found. Rename the local branch without changing HEAD.
- [x] (2026-09-27) Draft distribution contracts and newly proposed roadmap
      tasks.
- [x] (2026-09-27) Complete pinned upstream evidence and independent expert
  review; incorporate all planning remedies. Runtime feasibility remains EP-M1.
- [x] (2026-09-27) Pass planning document gates; prepare reviewed documents
  for commit and draft PR submission. Delivery is evidenced by PR metadata.
- [ ] Receive explicit plan approval; record approving message and revision.
- [ ] EP-M1: upstream feasibility and compatibility recipe.
- [ ] EP-M2: authenticated pin/manifest contracts.
- [ ] EP-M3: isolated installation, identity checking and execution.
- [ ] EP-M4: guarded workflow and immutable publication state machine.
- [ ] EP-M5: non-publishing smoke evidence and operations documentation.
- [ ] EP-O1: separately authorized live bootstrap and first publication.

## Surprises & discoveries

The package metadata requires Python 3.14, whereas generic scripting/rules
mention 3.13. The CLI design and developers' guide mandate Cuprum while older
scripting examples use Plumbum. Follow actual metadata and the feature-specific
command architecture, and clarify relevant documentation after approval.

No tracked `tools/kani/VERSION` exists here: the package reads that path in
consumer repositories, and tests create temporary pins. Do not create a fake
live snapshot pin merely to populate the package repository.

The Python release workflow creates a public release before uploading wheels.
That is incompatible with enabling immutable assets before a narrowly scoped
lifecycle adjustment. No repository-wide immutable setting was verified live or
changed during planning.

CodeGraph, Context Pack and Firecrawl Model Context Protocol (MCP) calls were
attempted and returned `user cancelled MCP tool call`. No graph results or
context-pack exchange are claimed. Firecrawl succeeded during the final
upstream research pass on pinned raw source; initial cancelled calls are not
counted as successful research. Wyvern agents also used local reads and primary
GitHub source/run evidence. Leta's workspace index also encountered a read-only
filesystem. These tooling limitations do not authorize skipping approval or
verification.

## Decision log

2026-09-27, proposed D1: use a separate `kani snapshot` command group plus
`kani run --snapshot-pin`, avoiding overloaded legacy semantic-version flags.
Coexisting pin files are allowed; contradictory active selectors are rejected.
No private source-API compatibility scaffolding is needed. The user's explicit
legacy CLI and persisted `VERSION` contract is the compatibility commitment.

2026-09-27, proposed D2: checked-in manifest hash plus mandatory build
attestation. Checksums cannot authenticate themselves. Sign the manifest after
payload hashing and keep the attestation outside its digest graph.

2026-09-27, proposed D4: install into a permanent unreferenced generation, then
activate a small atomic pointer. This preserves setup-created absolute links
and working generations while allowing recovery without deleting live paths.

2026-09-27, proposed D5/D6: attest build origin before clean validation, then
publish only with successful same-run proof evidence. Test public offline
transport before publication and actual public HTTP transport during bootstrap.
Neither a build attestation nor a zero-harness exit is proof success.

## Outcomes & retrospective

Planning review is complete; implementation and publication have not started.
Independent reviews covered all five requested specialities, with amendments
recorded in the design and checked by a final testing/consistency review.
Planning validation passed `make fmt`, `make markdownlint` (including spelling
and three spelling-helper tests), `make nixie`, and both working/staged
`git diff --check`. Earlier dependency-network and document spelling/line-wrap
failures were resolved without weakening gates. Only four Markdown files
changed during initial planning. The later requested rebase validation ran the
full gate stack, as recorded below; implementation gates still apply to each
future milestone. The associated draft PR records the commit, branch, upstream
tracking and delivery link. Add red/green, smoke and conformance evidence only
after approval. Only mark implementation roadmap entries complete when their
actual acceptance criteria are met. Leave EP-O1 and roadmap 1.2.1 open for
operational work. Before status `COMPLETE`, reconcile discoveries with the
approved design, requirements and every evidence link; do not conceal an
unresolved deviation.

## Context and orientation

The baseline is `6b7e18351c3a1deee251249901bd2cadb96d7f7c` on the original
`feat/plan-kani-snapshots` branch, initially identical to local `origin/main`,
with a clean worktree. No associated issue was supplied and no prior PR for the
original branch was found. No existing roadmap item number is reused;
`docs/roadmap.md` introduces 1.1.1–1.1.5 and separate 1.2.1.

The planning branch was rebased without conflicts onto `origin/main` at
`07cc5214284841a8113464aff16873a14e1eae74` on 2026-09-27. Only the original
planning commit was replayed; `git range-diff` confirmed an unchanged patch.
Main's dependency manifest and lockfile were retained without conflict or
regeneration. Its release action version changed, but publish-before-upload
ordering remains.

Reuse main's `tests/workflow_contracts/loading.py::load_workflow` and
`reading.py` trigger/job readers for snapshot workflow tests. They reject
duplicate keys, handle both YAML spellings of `on`, and refuse malformed or
empty subjects rather than allowing vacuous success. Follow the existing pure
rule, repository assertion and mutated-refusal-fixture pattern. Preserve
coverage publication exclusively through main's CodeScene environment and
existing mutation-testing contracts; snapshot workflows must not acquire those
credentials or become another coverage publisher.

Main now runs `mdtablefix --check --git --include-untracked` inside
`make check-fmt`; `make fmt` applies the same tracked/unignored-file selection.
Use those targets rather than the former `mdformat-all` helper. Pylint runs
through uv-managed PyPy 3.12 with pinned Pylint 4.0.9 and `syntax-error`
enabled, not the retired shim. Keep new Python syntax parseable by that gate
without suppressions; the package runtime remains Python 3.14. Recheck current
pins through the Makefile when implementation starts.

`rust_prover_tools/cli.py` constructs the Cyclopts apps. `install` and
`check_version` build `KaniInstallOptions` and `KaniCheckOptions` and map
`ProverToolError` to exit 1. `rust_prover_tools/kani.py::install_kani` runs
`cargo install --locked kani-verifier --version`, `cargo kani setup`, then a
version probe. `check_kani_version` compares only the first semantic version.
`rust_prover_tools/versions.py` supplies pure pin and version helpers; its
checksum lookup is intentionally too permissive for authenticated snapshots.

`rust_prover_tools/commands.py::run_command` wraps Cuprum with `CommandSpec`
containing tuple arguments, environment, working directory and echo selection.
`run_checked` raises on nonzero exit. Confirm real environment removal and
timeout semantics before extending it; do not bypass this sole process boundary.
`rust_prover_tools/verus/install.py` has bounded curl downloads but a
non-atomic delete/move and unchecked unzip path; do not reuse these as the
snapshot safety contract.

Unit tests live in `rust_prover_tools/unittests/`; relevant files are
`test_kani.py`, `test_versions.py`, and command adapter tests. Public behaviour
uses `tests/features/prover_tools_cli.feature` and
`tests/steps/test_prover_tools_cli_steps.py`; Syrupy output is in
`tests/test_prover_tools_snapshots.py`. `tests/test_workflow_contract.py`
establishes full-SHA shape assertions. The existing development group includes
pytest, cmd-mox, pytest-bdd, Syrupy, Hypothesis and PyYAML.

`.github/workflows/release.yml` builds a pure wheel and publishes on `v*.*.*`.
The new `kani-snapshot-*` namespace must never trigger it. `Makefile` supplies
gates and spelling generation; `typos.toml` is generated and must not be edited
by hand. No standalone complexity document was found. `pyproject.toml` requires
McCabe complexity at most 8, bounded function arguments/locals and 400-line
modules. Split cohesive feature concerns rather than suppress limits.

## Conformance basis

The governing requirements are the user's six numbered distribution sections
and delivery instructions dated 2026-09-27, summarized here as U1 (identity,
compatibility and provenance), U2 (upstream packaging), U3 (consumer safety),
U4 (workflow/publication), U5 (verification) and U6 (documentation/delivery).
There is no separate approved Terms of Reference or snapshot ADR. The proposed
technical design is `docs/kani-snapshot-distribution-design.md`, revision 3,
D1–D6. Plan approval ratifies both this revision and those concrete contracts;
future departures require a recorded decision and explicit acceptance.

The local standards are `AGENTS.md`, all `.rules/python-*.md`,
`docs/prover-tools-cli-design.md`, `docs/users-guide.md`,
`docs/developers-guide.md`, `docs/scripting-standards.md`,
`docs/documentation-style-guide.md`, and
`docs/local-validation-of-github-actions-with-act-and-pytest.md`, at the
recorded planning baseline. Actual metadata/gates resolve the older generic
guidance as noted above. External assumptions and immutable source links belong
in the upstream evidence section below, refreshed only deliberately at EP-M1.

Traceability is selective and behavioural:

- U1 → D1/D2 → EP-M2 → V1/V2 pin and provenance evidence.
- U2 → D3 → EP-M1/EP-M4 → V6 upstream recipe and runtime evidence.
- U3 → D4 → EP-M3 → V2/V3/V4/V5 safe installation and exact execution.
- U4 → D5 → EP-M4 → V7 release/workflow transition evidence.
- U5 → D6 → EP-M3/EP-M5 → V1–V8 tests and non-publishing clean smoke evidence.
- U6 → D6 → EP-M5 → V8 operations handoff; EP-O1 → V9 actual publication.

## Distribution contracts to implement

The public proposal is:

```bash
prover-tools kani snapshot install --pin tools/kani/snapshot.toml
prover-tools kani snapshot check --pin tools/kani/snapshot.toml
prover-tools kani run --snapshot-pin tools/kani/snapshot.toml -- --harness bounded_push
```

`install`/`check` default to that pin under `--repo-root`; `run` requires an
explicit snapshot pin and does not install implicitly. All accept `--cache-dir`
(default `.kani-snapshots` under the root). `install --asset-dir PATH` uses
local files through identical verification logic. New flags do not inherit
generic environment selection. Reject active legacy version/command selectors
in snapshot calls; ignore mere coexistence of `VERSION`. Ratify exact help and
separator behaviour in Syrupy and real CLI tests before implementation.

Version 1 pins require repository `leynos/rust-prover-tools`, schema version,
unique immutable tag, full upstream and builder SHAs, target, and trusted
manifest SHA-256. Reject unknown/duplicate keys, wrong types, non-full SHAs,
unsupported hosts/targets, and inconsistent fields. The tag format is
`kani-snapshot-YYYY-MM-<12-hex-upstream-prefix>-rN`; changed inputs receive a
new positive revision. Tags identify the builder commit; the manifest
separately records the upstream commit. Preserve upstream package version
metadata.

Manifest exact bytes bind payload roles/names/sizes/hashes, a separately hashed
64 MiB maximum member-inventory payload, full source/builder/submodule
identities, toolchain and actual compiler, backend/solvers, runtime
compatibility, locks/recipe/prerequisites, adjustments, workflow run/attempt
and resource measurements. Produce payloads → manifest → checksums → manifest
attestation. The checksums list payloads and manifest; the manifest excludes
itself, checksums and attestation. The attestation signs the manifest and is
cryptographically verified. No circular hashes exist.

Require the consumer-reviewed manifest hash and a verified GitHub attestation
matching canonical repository, trusted workflow, `refs/heads/main`, pinned
builder/source/signer SHAs, GitHub issuer and expected provenance predicate.
Invoke prebuilt `gh attestation verify --bundle` through Cuprum, with explicit
policy flags and supported trust roots; no bespoke cryptography. Validate
hashes and signature before extraction/execution. No optional verification
bypass exists. Pin review and trusted GitHub/builder/upstream infrastructure
remain assumptions; origin is not reproducibility or proof correctness.

Use bounded HTTPS, per-redirect host validation, no cross-host credentials,
streaming byte limits and complete archive/link graph validation. D4's limits
are 4 MiB manifest, 16 MiB attestation, 1 MiB checksums, 2 GiB compressed per
asset, 8 GiB extracted total, 100,000 members, 4 KiB paths and 16 link hops.
Reject traversal, escaping/cyclic/dangling links, duplicates, sparse/special
entries and privileged mode bits; retain contained links and executable modes.
Use 15-second connect, 300-second per-asset and 30-minute overall install
bounds, at most three transient attempts and five validated redirects.

Cache keys hash complete build identity and target. Under a per-key bounded
`flock`, stage into a permanent unique generation, set private Kani/Rustup
homes, setup from the validated local bundle, verify identities, flush state
and atomically replace `active.json`. Never relocate a setup home or delete
working generations. Recheck receipt, inventories and effective compiler before
execution. Keep immutable payload/toolchain files separate from narrowly
inventoried mutable bookkeeping, lock and scratch paths; two concurrent proofs
must not alter immutable trees or the user's normal Kani/Rustup homes. Resolve
absolute launchers; remove conflicting inherited selection variables and reject
forwarded upstream installation/identity replacement flags. Preserve ordinary
arguments, stdout/stderr and verifier exit status; never fall back to PATH,
another release or source compilation.

Use a separate monthly/manual workflow with schedule `23 4 3 * *`, disabled
unless `KANI_SNAPSHOTS_ENABLED` equals `true`. Dispatch inputs are full
`upstream_commit`, `publication_id`, and boolean `publish` default false. Only
canonical-repository default-branch schedule/dispatch runs may progress.
Resolve full candidate SHA once using successful named upstream checks; cache
and skip decisions include recipe/builder inputs. Separate source build,
trusted attestation, clean proof validation and release writing. Scope tokens
per job; attestation jobs alone get signing permissions, publication alone gets
release writes. Artefacts are from the same trusted run/attempt, authenticated
by exact IDs and digest receipts; never execute artefact code in privileged
jobs.

Full-SHA Actions, no PR source builds, isolated writable caches, no paid runner
upgrade, bounded jobs (10/180/15/45/15 minutes by stage), recorded
disk/time/cache and artefact measurements, and serialized non-cancelling
publication are required. Create draft pre-release at builder SHA,
upload/verify all assets, then publish with `make_latest: false`. Resume only
identical same-run/attempt validated drafts, never clobber. Published matching
identities are verified no-ops; conflicting/unknown drafts need new identities.
Adjust only the Python wheel release lifecycle needed before immutable-release
bootstrap.

### Reviewed boundary refinements

Execution uses `cwd=repo_root`, defaulting to the caller's directory,
regardless of pin location. Verify actual Cuprum environment
replacement/removal without mutating global environment, bounded
capture/streaming, separate streams, deadline propagation and descendant
cleanup. Tiny real child-process tests must establish these properties before
the snapshot adapter relies on them. `uv.lock` currently resolves Cuprum 0.1.0;
supported semantics are unverified. A dependency change or bypass requires
deviation approval.

Preflight trusted prebuilt GitHub CLI/Rustup and linker/runtime packages.
Rustup's distribution transport must use official authenticated endpoints, with
inherited `RUSTUP_DIST_SERVER`, `RUSTUP_UPDATE_ROOT`, `RUSTUP_DIST_ROOT`,
Cargo/Rustup homes and configuration removed or fixed. Record component URLs,
lengths/digests, channel metadata digest and extracted compiler/library hashes
in the trusted manifest/inventory. Verify compiler bytes before executing a
probe. Set `RUSTUP_AUTO_INSTALL=0` for check/run; missing toolchains must fail
without network repair. Prove ordering with the real setup path in EP-M1.

Serialize the workflow before resolution/identity allocation and recheck before
release creation. The default pending slot may be replaced by a later run;
monitor missed/cancelled work. Input fingerprints include source/submodules,
builder/recipe, toolchain, locks, target and prerequisite digests, but exclude
run/attempt/timings/cache observations. A new run with unchanged inputs is a
verified no-op; a changed recipe requires another identity. `publish=false`
still creates an attestation; it means no release publication. A new GitHub
attempt cannot resume an old incomplete draft. Reconcile publish timeouts by
reading state; preserve generated Python notes and require a non-empty complete
wheel inventory before finalizing any Python release.

## Verification plan

An invariant is a property preserved by every allowed operation. A lemma is an
intermediate contract connecting such properties to observable behaviour.
External axioms below are trusted interface assumptions, not proven internals.
Each V obligation needs recorded failing evidence before production changes,
passing evidence afterwards and a negative control demonstrating meaningful
failure detection. No optional CrossHair proof is proposed: properties plus
boundary tests address these obligations more directly.

### V1: Pins and identity are unambiguous

Invariant: valid pins/manifest models round-trip without changing complete
identity, and invalid/contradictory selectors cause no effects. Lemma: equality
of canonical cache-key input implies equality of every selected identity field;
semantic-version equality alone does not. Add
`rust_prover_tools/unittests/test_kani_snapshot_pins.py` and
`test_kani_snapshot_manifest.py`. Use finite pytest partitions for versions,
keys, types, hosts and targets, and Hypothesis construction-based round trips
and field mutations. Run `uv run pytest -q -k kani_snapshot_pins` and the
manifest equivalent. Include two valid same-semantic-version pins with
different full SHAs and tags; changing any identity field must change the key.
Record generated valid/invalid classifications and avoid filters hiding edge
cases. A seeded implementation using semantic version as key must fail the
witness.

### V2: Authentication precedes trust

Invariant: neither extraction nor payload execution occurs before exact-byte
manifest hash, signature policy and asset length/digest validation succeed.
Lemma: verified manifest plus validated payload digest binds the executed bytes
to the reviewed pin, within the local-owner trust assumption. Add
`test_kani_snapshot_integrity.py` with cmd-mox ordering, malformed JSON,
duplicate keys, changed byte/newline, wrong signer/source/ref/run, missing
asset, tampered checksum, hostile redirect and bounded streaming cases. Run
`uv run pytest -q -k kani_snapshot_integrity`. Use a valid signed fixture
through a faithfully modelled CLI response plus EP-M5's real verifier boundary.
Include wrong Rustup mirror/component bytes and missing-toolchain cases; assert
no compiler probe or auto-install can occur. A negative control that skips
signature/hash validation must reach a forbidden execution spy and fail. Mocked
signature success does not prove real signing.

### V3: Extraction stays contained

Invariant: all writes and links remain inside the private extraction root,
ordinary executable modes/contained links survive, and resource limits are
respected. Lemma: a validated member graph plus link-last no-follow
materialization prevents a link member redirecting later writes. Add
`test_kani_snapshot_archive.py`; combine named tar fixtures and Hypothesis
member graphs (up to 30 members and 16 link hops), with an independent
expected-path oracle and actual filesystem assertions. Run
`uv run pytest -q -k kani_snapshot_archive --hypothesis-show-statistics`.
Witness regular files, contained hard/symbolic links and executables. Negative
controls include parent traversal, link-chain escapes, cycle, duplicate path,
special file, huge declared size and actual stream overrun. Apply effective
GNU/PAX header overrides, file/directory collisions and member-order
permutations; hard links must target contained regular files. Removing
containment checks must change an outside sentinel or trigger a forbidden-write
assertion. Generated cases complement, but do not prove, arbitrary filesystem
safety.

### V4: Activation is failure-safe and isolated

Invariant: active state refers only to a complete validated generation;
interruption never damages the previous active generation; same-version builds
have disjoint homes and toolchains. Lemma: locked generation creation and
atomic pointer replacement produce at most one committed active pointer while
readers retain stable generation paths. Add `test_kani_snapshot_install.py` and
`tests/test_kani_snapshot_concurrency.py`. Hypothesis stateful tests model two
identities, two actors and up to 30 operations (download, stage, fail,
validate, activate, corrupt, retry). Real two-process tests use barriers, not
arbitrary sleeps, to force overlapping installs, a killed staging owner and
lock timeout. Run `uv run pytest -q -k kani_snapshot_install` and
`uv run pytest -q tests/test_kani_snapshot_concurrency.py`. Both first-install
failure and repair with an old working generation must be reachable. A seeded
premature pointer write or post-setup directory move must fail; otherwise the
model is insufficient. POSIX locking/rename durability and private cache
ownership are axioms tested at the integration boundary, not formally proven.

### V5: Execution selects the exact distribution

Invariant: absolute launcher/backend/toolchain paths correspond to the pinned
manifest regardless of earlier PATH entries; forwarded verifier arguments and
stdout/stderr/status survive. Add `test_kani_snapshot_run.py`,
`test_kani_snapshot_identity.py`, `tests/test_kani_snapshot_cli.py`, and
`tests/test_prover_tools_snapshots.py` variants. Use cmd-mox and real CLI fake
executables with spaces, shell metacharacters, conflicting environment,
identical upstream semantic versions, wrong compiler/backend, missing setup
state and distinct child statuses. Run `uv run pytest -q -k kani_snapshot`.
Assert no `cargo install`, source checkout, solver build or implicit
installation command during install/check; forbid unwanted PATH Kani via an
executable sentinel. Include real environment removal, cwd, streaming and
descendant timeout cleanup tests. Normalize temporary directory prefixes only
in Syrupy; retain SHAs, digests, target, compiler and differing failure output.
Switching to PATH or masking a nonzero verifier status must fail named tests.

### V6: The recipe uses one upstream identity

Invariant: source, launchers and backend derive from one full SHA with
unchanged locks/toolchain pins and recorded recursive submodules. Add
`test_kani_snapshot_build.py` for command sequencing, immutable resolution,
malicious manual inputs, unchanged HEAD with changed recipe, missing/failed
upstream checks, runtime/licence completeness and resource-limit rejection. Run
`uv run pytest -q -k kani_snapshot_build`; compare before/after source status
and input hashes in the opt-in EP-M1/EP-M5 experiment. The negative control
resolves a different SHA at a later phase or drops a submodule record; the
recipe must reject it. A mock cannot establish upstream bundling support; real
clean setup and smoke evidence discharge that external boundary.

### V7: Only complete validated releases become public

Invariant: only trusted same-run/attempt artefacts with successful clean
validation can publish; no published asset/tag is mutated. Lemma: the pure
release-state transition policy never returns a publish action while expected
assets are absent, mismatched or unverified. Add
`test_kani_snapshot_publish.py`, `tests/test_kani_workflow_contract.py`, and
`tests/test_python_release_contract.py`. Use cmd-mox and a deterministic HTTP
release adapter with failure injection before/after each API operation,
including publish timeout, asset collision, extra assets, wrong tag and retry.
Model absent/draft/complete/published/conflict states with up to 30 generated
operations and named transition witnesses. Run
`uv run pytest -q -k 'kani_snapshot_publish or workflow_contract or python_release'`.
A seeded publish-before-upload or accepting cross-run receipt must fail.
Assert trigger namespace, scheduled disable default, dispatch validation,
permissions, SHA pin shape, no PR/fork writes, timeout/cache/concurrency
contracts and unchanged Python wheel namespace. Run actionlint and optional act
per EP-M4; real GitHub permissions/signing and immutable enforcement remain
hosted-only assumptions.

### V8: Successful verification means harnesses ran

Invariant: the clean smoke environment has no Kani checkout/install and
consumer setup compiles no tool sources; each required harness runs with the
expected result on the effective Kani compiler at least 1.94. Add
`tests/fixtures/kani_snapshot/` with positive bounded collection/cleanup,
deliberate false assertion and compiler-floor crates. Add a tested output
validator in the builder package and
`tests/integration/test_kani_snapshot_smoke.py`, explicitly opt-in. Test output
parsing offline in ordinary tests. Use slice `array_windows::<2>()`, stabilized
in 1.94, in a named Kani harness. The Rust-1.93 control runs the same pure
function without Kani annotations or Cargo minimum-version gating and must fail
for the unavailable API. Trace setup's descendant processes with bounded
execution tracing: version probes and prebuilt toolchain install are permitted;
source compilation/code generation before the proof phase is rejected.
Command-adapter spies alone are insufficient. Treat empty harness sets, compile
errors, solver launch failure and ambiguous output as failure, including for
the negative fixture. Require exact named harness sets and counts, not just a
nonzero process result. Named success/assertion failure, zero-harness,
compile-error, malformed/truncated and contradictory status/output transcripts
are mandatory controls. Record cold install, warm repeat, digest/identity
rejection and actual public CLI proof commands.

### V9: Bootstrap is separately observable

EP-O1 alone discharges actual public release verification and scheduler
settings. Its evidence is release URL, immutable tag/builder commit,
attestation result, cold public-download installation and proof transcript,
repository variable state and assigned monitoring owner. A merged
implementation with no public release is a deliberate negative witness: roadmap
1.2.1 must stay unchecked.

### External axioms and residual gaps

Trust SHA-256 collision resistance, supported GitHub signing and CLI
verification, GitHub token/runner isolation, POSIX locks/atomic rename on the
same local filesystem, Rustup's verified prebuilt downloads and inspected Kani
setup interfaces. Validate repository-owned configuration at each real
interface; do not claim to verify third-party implementations. Local act cannot
certify OpenID Connect (OIDC) permissions or hosted trust. Tests assume an
owner-private cache and trusted proof crate; they do not sandbox a hostile
same-user process. Pinned inputs establish traceability; no bit-for-bit
reproducibility theorem or full consumer-repository compatibility claim follows.

## Milestones and plateaus

### EP-M1: Establish the upstream recipe (prototyping)

Requires explicit approval. Discharges D3/U2 feasibility and prepares V6/V8.
Read the full-SHA upstream evidence below, confirm successful upstream Linux
checks, toolchain floor and exact bundle/launcher/setup commands. Add the
smallest failing recipe/metadata tests before the Python recipe skeleton. Run
the bounded experiment outside `/tmp`; keep output binaries/checkouts
untracked. Record runtime dependencies, notices, actual compiler identities,
setup links and write locations, component pre-execution integrity, actual
Cuprum deadline/environment support and build/disk/cache measurements. Freeze
the supported attestation command/result schema and verifier/trust-root
version; wrong workflow, builder/ref/run and a valid signature for another
manifest must be rejected. Promote only a supported recipe; discard an
unsuitable prototype and record why. Do not publish.

Plateau: a documented, tested recipe and admissible candidate with explicit
limitations; no public installer promise yet. Conformance checks all source
pins, distribution layout, compiler floor and redistribution conditions.
Recovery is to retain logs and untracked evidence, select another reviewed full
SHA or obtain deviation approval; never patch locks silently. Remaining gaps
are authenticated consumer install, automation and clean validation.

### EP-M2: Define pure identity and authentication contracts

Implements D1/D2 and V1/V2. Add frozen models and pure parsers under
`rust_prover_tools/kani_snapshot/{models,pins,manifest}.py`, plus the integrity
policy helper. Proposed interfaces are `parse_pin(data: bytes) -> SnapshotPin`,
`parse_manifest(data: bytes) -> SnapshotManifest`,
`validate_binding(pin, manifest) -> None`, and a deterministic identity-key
function accepting a validated pin. Group policy arguments into typed records
to respect argument limits. Keep untrusted JSON separate from validated models.

Write tests before adding each unit. Preserve legacy tests unchanged. Plateau:
usable pure contracts and authenticated manifest verification with no exposed
incomplete installer. Conformance checks D1 syntax/schema and D2 trust anchors.
Recovery is ordinary code revert, with no persistent migration yet. Remaining
gaps are payload transport/extraction, setup and execution.

### EP-M3: Deliver a complete isolated consumer feature

Implements D4 and V2–V5. Add bounded `download.py`, `archive.py`, `install.py`,
`identity.py`, `run.py`, and typed install/run options. Proposed orchestration
interfaces are `install_snapshot(options) -> SnapshotIdentity`,
`check_snapshot(options) -> SnapshotIdentity`, and
`run_snapshot(options) -> CommandResult`. Keep installation transitions and
archive graph validation independently testable. Extend commands only where
real timeout/environment tests prove necessary. Add thin CLI registration in a
cohesive snapshot CLI module if needed to keep the existing 400-line module
within limits; update registration and callers together, without aliases or
compatibility facades for private helpers.

Write failing BDD, archive, concurrency and identity tests first, implement the
smallest passing feature, then refactor and run full gates. Update users' and
developers' guides and CLI design to distinguish legacy and snapshot commands.
Plateau: public installation/check/run works with deterministic fixture assets,
including local transport; no remote snapshot is promised. Conformance checks
exact paths, no fallback/compilation, signature policy and atomic activation.
Recovery preserves working generations and permits a new validated generation;
never rewrite a pin automatically. Remaining gap is actual producer workflow.

### EP-M4: Add producer and release lifecycle

Implements D3/D5 and V6/V7. Add tested Python build/resolution/manifest/receipt
and release-policy modules inside `rust_prover_tools.kani_snapshot`; add thin
`.github/workflows/kani-monthly.yml`. Expose a developer module entrypoint for
`resolve`, `build`, `assemble`, `validate` and `publish`, with typed file
inputs from earlier jobs; these are internal automation interfaces, not
consumer APIs. Each validates its input and same-run binding before effects.
Add Makefile `workflow-check` (actionlint plus workflow contract tests) and
opt-in `kani-snapshot-smoke` targets. Ordinary `make test` uses no real builds.

Write transition/workflow tests first; then implement monthly/manual guards,
job permissions, exact artefact routing, immutable tags, attestations, budgets
and draft recovery. Fix only the required Python release lifecycle, tests and
Action pins touched by that adjustment; keep wheel namespace/content stable.
Plateau: default-disabled scheduled capability, manual validation mode and
publication adapter pass deterministic gates, with settings unchanged.
Conformance checks U4 permissions, authenticated inputs, lifecycle and Python
namespace. Recovery is a normal code revert or corrected new publication
identity, never asset replacement. Real smoke/hosted evidence remains EP-M5.

### EP-M5: Validate real assets and deliver the runbook

Implements D6/V8 and prepares V9, without release publication or settings
changes. The mandatory attestation policy trusts builder code on `main` only.
Merge the approved, tested implementation with the schedule still disabled
before this hosted validation; a branch-only mock cannot satisfy EP-M5. If that
merge or dispatch is unavailable, leave EP-M5 and roadmap 1.1.5 open. This
sequencing does not authorize publication or operational bootstrap. Build once
through the explicit non-publishing integration path, then install the produced
consumer wheel/assets/attestation in a fresh environment without upstream
checkout or old Kani. Exercise the exact public commands, upstream bundle smoke
tests, named positive/false/compiler-floor harnesses, warm cache and deliberate
identity/digest failures. Record source build versus consumer setup versus
proof compilation separately. Repeat-build costs stay within EP-M1 tolerances;
same-version regression uses small deterministic assets.

Write `docs/kani-snapshot-operations.md` with the exact bootstrap handoff, and
synchronize users' guide, developers' guide, CLI and snapshot designs. Include
required GitHub settings/permissions, schedule variable and all dispatch
inputs, identity allocation, attestation CLI prerequisite/policy, cold
commands, draft recovery, rollback, retention and recurring monitoring.
Document source pins, measured runtime floor and scheduler/OIDC limitations.
Update this plan's red/green and smoke transcripts and close only discharged
implementation roadmap entries. Plateau: tested implementation plus actual
non-publishing smoke evidence and complete operations handoff. If hosted
validation is not available, record it as outstanding rather than declaring
completion.

Conformance checks every U requirement and unresolved assumption, no extra
platform/dependency, and implementation-versus-bootstrap distinction. Recovery
is to leave schedule disabled and preserve diagnostic artefacts. EP-O1 remains
open even after implementation merge.

### EP-O1: Operational bootstrap (separate authorization)

Not part of this planning invocation or implementation completion. The operator
first checks merged builder SHA, repository access, runner allowance, Action
allowlist, job token/signing support and draft-first Python releases. Enable
repository-wide immutable releases only under this separate authorization. Keep
the schedule variable false during manual validation and first publication.
Dispatch on `main` with explicit upstream full SHA, unused publication identity
and `publish=false`, inspect evidence, then dispatch the authorized publication
with an appropriate new identity if build inputs/run changed. Record exact
run/attempt and release URL.

Download published files through the ordinary public consumer path in another
fresh environment, verify release and build provenance, compiler/harness
results, immutability and non-latest pre-release status. Only then set
`KANI_SNAPSHOTS_ENABLED=true`. Assign a monitoring owner to review monthly
success, missed runs, cache/cost/disk changes and upstream breakage. Recovery
is to disable scheduling, diagnose an incomplete draft without deletion, and
select an older immutable consumer pin. Close roadmap 1.2.1 only with V9
evidence.

## Behavioural specification

Create `tests/features/kani_snapshot.feature`, bound from
`tests/steps/test_kani_snapshot_steps.py`. Expand the representative
specification below with each failure partition named in V1–V5; use fixture
identities, not real network builds, in ordinary pytest-bdd runs.

```gherkin
Feature: Install and execute a pinned Kani snapshot
  Scenario: A cold binary install selects the exact snapshot
    Given a trusted pin and authenticated matching launcher and backend assets
    And another Kani precedes the selected launcher on PATH
    When the public snapshot install and check commands run
    And the public Kani run command invokes the named proof harness
    Then no Kani or solver source compilation occurs during installation
    And the selected paths and compiler match the complete pinned identity
    And the named harness result and verifier exit status are preserved

  Scenario: Integrity failure preserves the previous working installation
    Given an active validated snapshot generation
    And a staged download with a bad digest
    When snapshot installation is retried
    Then installation fails before payload extraction or execution
    And the previous generation remains active and usable

  Scenario: Two snapshots share an upstream semantic version
    Given two authenticated snapshots with one reported semantic version
    And different complete build identities
    When both snapshots are installed concurrently
    Then each has independent Kani and toolchain paths
    And repeated installation validates and reuses its own cache

  Scenario: A negative proof must actually be verified
    Given an installed snapshot and a deliberate false assertion harness
    When the harness is verified through the public Kani run command
    Then that named harness runs and reports verification failure
    And a compilation error or zero discovered harnesses is not accepted
```

Add explicit scenarios for missing assets, unsupported platforms, contradictory
pins, invalid attestation, wrong compiler/backend, partial installs, lock
timeout, interrupted activation, corrupted warm caches and unsafe archives.

## Concrete steps and acceptance commands

Run from the repository root. Commands below describe future approved work; new
paths/targets/commands do not exist at planning time. For each code unit, write
the relevant test first, run its focused command to record the expected
failure, implement, rerun to pass, then refactor. Strict expected failures may
be used during the red stage only; remove them for final gates. Initial failure
must specify the missing behaviour, not an unrelated dependency failure.

Run the following gates sequentially after each major implementation milestone
and before each code commit, using `set -o pipefail` and `tee` per command with
separate `/tmp/<gate>-rust-prover-tools-kani-monthly-snapshot-distribution.out`
logs. Delegate full gates to Scrutineer; inspect cited failure logs before
retrying, and never run gates concurrently.

```bash
make check-fmt
make typecheck
make lint
make test
make markdownlint
make nixie
mbake validate Makefile
make workflow-check
```

`make markdownlint` includes spelling and generated-config drift checks.
`make fmt` is required after Markdown edits; inspect formatter changes before
staging. Do not weaken gates or modify generated spelling configuration.
Makefile/workflow checks apply when those surfaces change; record absence of
optional local tools honestly. Planning-only gates are `make fmt`,
`make markdownlint` and `make nixie`, plus `git diff --check`; no runtime code
or workflow is added in this invocation.

Focused acceptance examples after their test files exist:

```bash
uv run pytest -q rust_prover_tools/unittests/test_kani.py
uv run pytest -q -k kani_snapshot --hypothesis-show-statistics
uv run pytest -q tests/steps/test_kani_snapshot_steps.py
uv run pytest -q tests/test_prover_tools_snapshots.py
uv run pytest -q tests/test_kani_workflow_contract.py tests/test_python_release_contract.py
act workflow_dispatch -W .github/workflows/kani-monthly.yml --list
```

`act --list` only checks job discovery. Add an opt-in pytest black-box act
harness with fake build artefacts, default `publish=false`, no real secrets,
pinned runner image and artefact/log assertions. Local act success does not
prove GitHub OIDC or permissions; inability to run act is a recorded gap, not
permission to claim hosted parity.

The future `make kani-snapshot-smoke` target must take `SNAPSHOT_PIN`,
`SNAPSHOT_ASSET_DIR` and `SNAPSHOT_FIXTURE_ROOT`, reject missing values, run
only explicit integration tests and never publish. It invokes the same public
commands as follows, with the fixture directory as working repository root:

```bash
prover-tools kani snapshot install --repo-root "$SNAPSHOT_FIXTURE_ROOT" \
  --pin "$SNAPSHOT_PIN" --asset-dir "$SNAPSHOT_ASSET_DIR"
prover-tools kani snapshot check --repo-root "$SNAPSHOT_FIXTURE_ROOT" --pin "$SNAPSHOT_PIN"
prover-tools kani run --repo-root "$SNAPSHOT_FIXTURE_ROOT" \
  --snapshot-pin "$SNAPSHOT_PIN" -- --harness bounded_push
```

Expected evidence includes selected tag/full SHAs/digest, absolute paths,
effective compiler at least 1.94, a positive named harness, a separately run
false assertion harness reporting verification failure, cache reuse, and
rejection of altered digest/identity before execution. Do not invent numeric
pass counts before tests run. Store concise transcripts and run URLs here; keep
full bulky logs as explicit integration artefacts.

## Idempotence and recovery

Planning edits are ordinary Markdown changes, committed after gates, with an
ordinary upstream-establishing push. An unexpectedly existing target branch
requires ancestry inspection, never force. A PR created before rename would
require GitHub branch-rename flow; none existed at the checked planning start.

Consumer retries acquire the stable key lock, validate any active generation,
and either reuse it or create a new generation. Interrupted staging never
becomes active. Release retries first observe actual state; same-run/attempt
missing draft assets may be added only after existing bytes match. Published
identities cannot be repaired in place. A new build receives a new revision;
rollback is a reviewed older pin, never a moved tag or mutable alias.

## Upstream evidence and unresolved experiments

Primary-source inspection resolved `model-checking/kani` once to
`4e31125fdb4f01fad95150415cbafe46e0587d66`. Named Release Bundle, Kani CI and
Kani Extra workflows succeeded for that exact commit on 2026-09-25.[^11] This
is not a claim that every commit status is green: the combined status also had
pending contexts. Record the selected required checks again during EP-M1.

At this revision, `.cargo/config.toml` maps `cargo bundle` to
`cargo run -p build-kani -- bundle`.[^4] The bundle action runs
`cargo bundle -- VERSION`, producing
`kani-VERSION-x86_64-unknown-linux-gnu.tar.gz`; its separate
`cargo package -p kani-verifier` output is launcher source, not a prebuilt
launcher.[^5] Build both proxy executables from this same revision centrally;
never delegate the workflow's `cargo install` step to consumers.

`tools/build-kani/src/main.rs` creates the upstream `kani-VERSION/` tree with
`bin/kani-driver`, `kani-compiler`, `kani-cov`, CBMC/GOTO tools and Kissat,
Kani libraries and a prebuilt sysroot, `rust-toolchain-version`,
`rustc-version`, and `license-notes.txt`.[^6] CBMC and Kissat resolve from the
builder's `PATH`; inventory actual executable digests and identities, not only
source labels. Preserve that layout and inventory runtime/shared-library needs.

`src/setup.rs` supports `cargo kani setup --use-local-bundle PATH` and
`--use-local-toolchain DIR`.[^7] It canonicalizes the bundle and extracts it
using external `tar --strip-components=1`. Validate the complete archive before
invoking setup: require the single expected `kani-VERSION/` root, validate
destinations, links and collisions after `--strip-components=1`, and remove
inherited `TAR_OPTIONS`. Retain verified bytes in private staging and ensure
setup can only re-extract those same bytes within the validated, bounded
generation. Test the actual tar implementation and destination rules; if
upstream setup cannot preserve D4's extraction contract, stop for a design
revision rather than bypass it. Setup links the toolchain, checking a supplied
local toolchain's `rustc --version` against the bundle's recorded string.
Python must additionally verify the complete effective compiler identity.

`KANI_HOME` chooses `$KANI_HOME/kani-$CARGO_PKG_VERSION`, which confirms the
same-version collision risk and need for a private home per complete snapshot
identity. `src/bin/kani.rs` and `src/bin/cargo_kani.rs` are proxy executables;
`src/lib.rs` selects the installed `bin/kani-driver`, prepends bundled paths
and sets `RUSTUP_TOOLCHAIN`.[^8] Permanent generation paths must preserve
setup-created links. The release workflow obtains a separate prebuilt Rust
nightly installation for local-toolchain smoke setup: the backend bundle does
not contain the complete Rust installation.[^9]

The toolchain pin is `nightly-2026-09-23`.[^13] The successful Release Bundle
run reports `1.100.0-nightly (6bb1652a0 2026-09-22)`, exceeding the requested
1.94 floor. This is upstream log evidence, not a local build or proof result.
EP-M1 must capture generated `rustc-version` and effective `rustc -vV`, then
execute the compiler-floor harness; nightly age alone is insufficient.
Upstream's Ubuntu 22.04 bundle-test job also succeeded; that is evidence for
its tested environment, not a measured minimum runtime baseline for this
distribution.

The setup action installs Rustup, distro prerequisites and shallow pinned
submodules. `.gitmodules` lists Firecracker, `tests/perf/s2n-quic` and Charon;
record recursive gitlink revisions, not their configured branch labels.[^10]
The Linux prerequisite scripts install build tools (including Bison, CMake,
Flex, GCC/G++, Make and Git), Z3, zlib, static cvc5 1.3.0, CBMC and Kissat;
versions come from `kani-dependencies` and the setup scripts.[^12] Distinguish
producer-only prerequisites from measured consumer runtime requirements;
consumers must never build these dependencies. Recheck all resolved binaries.
Redistribution evidence starts with root Apache/MIT licences and
`tools/build-kani/license-notes.txt`; audit each bundled component separately.
No upstream source build, installation or compatibility proof was performed
during planning. Runtime floors, safe setup re-extraction, full notice
completeness and measured build cost remain explicit EP-M1 experiments.

[^4]: [Pinned Cargo aliases](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/.cargo/config.toml).
[^5]: [Pinned bundle action](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/.github/actions/build-bundle/action.yml).
[^6]: [Pinned bundle builder](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/tools/build-kani/src/main.rs).
[^7]: [Pinned installer setup](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/src/setup.rs).
[^8]: [Pinned launcher implementation](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/src/lib.rs).
[^9]: [Pinned release workflow](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/.github/workflows/release.yml).
[^11]: Successful runs at the recorded SHA:
       [Release Bundle](https://github.com/model-checking/kani/actions/runs/36175991081),
    [Kani CI](https://github.com/model-checking/kani/actions/runs/36175991035),
    and [Kani Extra](https://github.com/model-checking/kani/actions/runs/36169676475).
[^12]: [Pinned Linux prerequisites](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/scripts/setup/ubuntu/install_deps.sh).
[^13]: [Pinned toolchain inspected with Firecrawl](https://raw.githubusercontent.com/model-checking/kani/4e31125fdb4f01fad95150415cbafe46e0587d66/rust-toolchain.toml).
[^10]: [Pinned setup action](https://github.com/model-checking/kani/blob/4e31125fdb4f01fad95150415cbafe46e0587d66/.github/actions/setup/action.yml).

Primary GitHub references consulted during planning cover immutable
releases,[^1] attestation verification,[^2] and schedule behaviour.[^3] They
establish service interfaces; actual repository settings remain unverified.

[^1]: [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).
[^2]: [GitHub CLI attestation verification](https://cli.github.com/manual/gh_attestation_verify).
[^3]: [GitHub schedule behaviour](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Interfaces, dependencies and skills

Use Python 3.14, Cyclopts, Cuprum and standard-library `tomllib`, `json`,
`hashlib`, `tarfile`, `pathlib`, `tempfile` and POSIX locking. Use existing
pytest, cmd-mox, pytest-bdd, Syrupy, Hypothesis and PyYAML. External
prerequisites are prebuilt GitHub CLI, Rustup and upstream-documented build
tools; versions and origins must be recorded. No runtime Python dependency
addition is preapproved.

Use `python-router` for each Python concern and its smallest appropriate
follow-on skill, `codegraph-mcp` and Leta for navigation when callable,
`execplans` to maintain this contract, `firecrawl-mcp` for primary upstream
research when callable, and `rust-router` for Rust fixture/packaging work. Use
Wyvern for bounded reconnaissance and the Logisphere community for independent
design review; record actual capability failures. Use Context Pack to exchange
code if available, never claim a cancelled call succeeded. Use `commit-message`,
`pr-creation` and en-GB Oxford spelling skills for delivery.

## Revision notes

2026-09-27: initial proposed contracts, verification obligations and milestones
written from the actual Python/CLI/release baseline. Independent review and
upstream source evidence remain to be integrated before planning delivery.

2026-09-27: revision 2 integrates independent Pandalump, Wafflecat, Buzzy Bee,
Telefono, Doggylump and Dinolump reviews across architecture, packaging,
release/security and test design. Boundary refinements and V2–V8 now specify
all requested remedies; EP-M1 retains unproven upstream/runtime feasibility.

2026-09-27: revision 3 completes pinned upstream source evidence and final
review. Clarifies trusted-main sequencing for EP-M5, shared feasibility build
budget, upstream setup re-extraction and newer main-branch integration. The
compiler floor is supported by upstream build logs but remains unproven by the
required repository-owned harness. Implementation approval remains pending.

2026-09-27: rebase integration preserves main's dependency and release changes
and records reuse of strict workflow readers, coverage trust boundaries,
tracked Markdown checks and the managed PyPy lint tier. This remains a
planning-only branch; rebase authorization does not approve implementation.

Rebase validation on 2026-09-27 passed `make check-fmt`, `make test` (227 tests
and one snapshot passed; 24 pytest-bdd deprecation warnings), `make typecheck`,
`make lint` (Ruff clean and Pylint 10/10), `make markdownlint` (including three
spelling-helper tests), `make nixie` and `git diff --check`. No source,
dependency, workflow or gate configuration was changed to obtain these results.
The follow-up documentation commit records main's integration requirements; PR
131 remains a draft awaiting explicit implementation approval.
