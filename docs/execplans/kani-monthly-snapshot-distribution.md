# Add monthly Kani snapshot distributions

Status: APPROVED for implementation (2026-11-09). Date: 2026-09-27. Design
revision: 4 (updated 2026-11-08).

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

Medium impact: child isolation can silently regress because `CommandSpec.env`
is an overlay, so omitting a key removes nothing. Every selector removal must
use a Cuprum `UNSET` marker and be checked by a real-child environment test,
not by inspecting the mapping the caller built.

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
- [x] (2026-11-09) Receive explicit plan approval; record approving message and
  revision. The user's 2026-11-09 instruction "Begin implementation" and "Mark
  the plan … as approved" ratifies revision 4, D1–D6 and milestones EP-M1–EP-M5
  within the stated tolerances. EP-O1 stays separately authorized.
- [ ] EP-M1: upstream feasibility and compatibility recipe.
- [x] (2026-11-09) EP-M2: authenticated pin/manifest contracts. Added
  `rust_prover_tools/kani_snapshot/{models,cache_key,pins,manifest,manifest_groups,render,validation,tags,strict_json,policy,integrity,attestation}.py`
  and the tests `test_kani_snapshot_{pins,manifest,integrity}.py` plus
  `tests/kani_snapshot_fixtures.py`. `ruff check`/`ruff format --check` are
  clean and the three focused suites pass (72 tests). Evidence covers V1 round
  trips and key mutation, strict duplicate/unknown/type rejection, exact-byte
  manifest digest binding, checksum agreement and the explicit
  `gh attestation verify` policy vector. V2 transport-ordering and
  bounded-streaming cases remain with EP-M3 `download.py`.
- [x] (2026-11-09) EP-M2 milestone review. Local CodeRabbit CLI 0.9.3 reviewed
  commits `8bc8c85`..`52b926b` against `14ed1f1` in three completed passes. All
  actionable findings were fixed before advancing: hostile JSON/TOML
  `ValueError`/`RecursionError` conversion, float-range overflow, mandatory
  attestation digests, unsafe or reserved asset names (shared through `policy`)
  and boolean schema-version coverage. The final pass reported only two style
  preferences (assert messages, loop parametrization) that no repository rule
  requires; they are recorded, not unresolved defects. All eight gates were
  re-run after the fixes and passed; `make test` reports 888 passed and 5
  skipped, and the snapshot CLI tests need `FORCE_COLOR` unset.
- [x] EP-M3: isolated installation, identity checking and execution.
  - [x] (2026-11-09) Established the upstream setup boundary from pinned source
    `model-checking/kani@4e31125fdb4f01fad95150415cbafe46e0587d66`:
    `cargo kani setup --use-local-bundle <file>` extracts a `kani-<VERSION>/`
    bundle with external `tar --strip-components=1` into
    `$KANI_HOME/kani-<VERSION>`, runs `rustup toolchain install <identifier>`
    under the effective `RUSTUP_HOME`, and creates an absolute
    `kani-<VERSION>/toolchain` symlink. The launcher proxy auto-runs setup only
    when that directory is absent, rewrites `RUSTUP_TOOLCHAIN`, prepends its
    own `bin`/`pyroot` to `PATH`, and ignores `RUSTUP_AUTO_INSTALL` during
    setup. These are the boundaries the installer must pre-validate, isolate
    and re-check.
  - [x] (2026-11-09) Added the snapshot download seam. `ReleaseUrlPolicy`
    accepts exactly one `https://github.com/leynos/rust-prover-tools/releases/`
    `download/<tag>/<asset>` URL and, after redirects, only the documented
    `objects.githubusercontent.com` and
    `release-assets.githubusercontent.com` hosts, with no credentials,
    non-default port, fragment or traversing path. The generic downloader now
    consults a URL policy before opening the first request and every redirect;
    `DownloadSettings` bounds transient retries (3 attempts, capped backoff)
    without retrying integrity, policy or local filesystem failures. 27 focused
    tests plus the 68 existing download/integrity tests pass.
  - [x] (2026-11-09) Added safe archive validation and extraction. The complete
    member graph is checked before materialization: normalized relative paths,
    effective GNU/PAX headers, duplicate and file/directory collisions, sparse
    and special members, privileged modes, required-root stripping, symbolic
    link escapes/dangling links/cycles and the link-hop ceiling, hard links to
    contained regular files, and the 100,000-member/8 GiB/4 KiB ceilings.
    Extraction writes files before links into a private sibling staging
    directory, applies modes through open descriptors, fsyncs content and
    metadata, rejects attacker-writable ancestors, and publishes by atomic
    rename, so a failed extraction leaves no partial tree. 40 tests pass.
  - [x] (2026-11-09) Added the snapshot member inventory. `inventory_model.py`
    defines the four member kinds, the frozen entry and inventory shapes, and
    the entry/byte/path ceilings; `inventory_parse.py` enforces the exact
    version 1 document shape, kind-specific key sets, ordinary permission bits,
    archive-equal path normalization, contained link targets and unique paths;
    `inventory_tree.py` compares a tree against it in both directions, including
    exact modes, file size and digest, symbolic-link text, hard-link inode
    identity, special-file rejection and bounded error previews. 30 focused
    tests pass; the full suite reports 985 passed and 5 skipped.
  - [x] (2026-11-09) Added the generation layout, execution environment and
    local receipt. `snapshot_layout.py` derives an absolute identity directory
    from the cache key and a uniquely named generation with private
    `KANI_HOME`, `RUSTUP_HOME` and `CARGO_HOME`. `identity.py` replaces those
    homes, pins `RUSTUP_TOOLCHAIN`, suppresses auto-install, removes inherited
    compiler/distribution selectors while preserving `RUSTC_WRAPPER`,
    `RUSTC_WORKSPACE_WRAPPER` and `RUSTDOC`, and refuses forwarded `setup`,
    `--use-local-bundle`, `--use-local-toolchain` and `-Z lean` (the finite
    upstream selector list confirmed from pinned launcher and argument source).
    `receipt.py` renders and strictly parses a canonical receipt binding the
    pin, manifest, observed `rustc -vV`/compiler text, complete asset set,
    generation paths and setup's absolute toolchain link, then validates it
    against the layout. 54 focused tests pass; the full suite reports 1039
    passed and 5 skipped.
  - [x] (2026-11-09) Implemented isolated install, check and run.
    `install.py` verifies the local transport in order, compares the backend
    archive with the inventory, extracts the launcher, runs the packaged setup
    with generation-private homes, probes compiler identity, writes a durable
    receipt and switches the active pointer under a per-identity `flock`.
    `install_state.py` owns activation and cache revalidation;
    `install_defaults.py` owns production setup and probe; `run.py` refuses
    backend-replacing arguments, revalidates and preserves the launcher's
    streams and exit status. `kani_snapshot_cli.py` registers `kani snapshot
    install|check` and `kani run --snapshot-pin`, and `cli_support.py` keeps
    the legacy CLI inside its module and argument limits. 30 focused tests
    pass: 13 install, 4 run, 5 CLI, 2 two-process lock and 6 BDD.
  - [x] (2026-11-09) Added behavioural coverage and shared release fixtures.
    `tests/kani_snapshot_bundles.py` builds complete local releases with a
    working `cargo-kani setup` stub, so `tests/features/kani_snapshot.feature`
    and `tests/steps/test_kani_snapshot_steps.py` install through the real
    CLI, check the generation, forward arguments, preserve the launcher exit
    status, refuse changed assets without deleting the previous generation,
    and refuse upstream setup and the Lean backend. A `gh` attestation stub is
    placed first on `PATH` only for these behavioural runs.
  - [x] (2026-11-09) Ran the full sequential gate stack with `FORCE_COLOR`
    unset: `make fmt`, `make check-fmt`, `make lint` (Ruff, Pylint 4.0.9 and
    interrogate at 100%), `make typecheck` (ty 0.0.92), `make test` (1072
    passed, 5 skipped), `make markdownlint` including spelling, and
    `make nixie`. Two integration defects were repaired rather than
    suppressed: the new shared `repo_root` helper was shadowed by CLI
    parameters of the same name, and `install.py` exceeded the 400-line
    module ceiling, so transport staging moved to `install_transport.py` and
    the state helpers became shared. Focused reruns after the repairs kept
    all 30 snapshot tests green.
  - [x] (2026-11-09) EP-M3 follow-up CodeRabbit CLI review. Local CodeRabbit
    CLI 0.9.3 reviewed the remediation commit `ad9a868..c2f8da0` (19 files)
    as `review_completed` with 6 further findings (2 major, 2 minor, 2
    trivial). All were verified against the source and repaired. Major: the
    two identity commands shared one stale budget, so the probe now
    recomputes what the first command left and rejects an expired budget;
    `validate_generation` measured the probe budget before slow cache
    revalidation, so it now takes a callable measured immediately before the
    probe and reuse is reported only after a final deadline check. Minor:
    `run_command` ignored an enclosing Cuprum scope's default timeout when
    choosing process-group ownership, and formatted a possibly-`None` budget
    on timeout; it now resolves the effective timeout once and reports
    `TimeoutExpired.timeout`. Trivial: the process-tree test now synchronizes
    on grandchild startup and verifies termination by process state, and the
    install budget test uses a controlled clock to assert each hook receives
    the reduced budget. Documentation was updated in the design decision D4,
    the users' guide and the developers' guide. A third CLI pass over
    `c2f8da0..884edde` reported one minor finding: the process-tree test
    treated an absent `/proc` as proof a descendant had exited. The helper
    now falls back to signal 0, which still detects a survivor (though not a
    zombie) when procfs is unavailable.
  - [x] (2026-11-09) Hosted CI repair after publication. The first hosted run
    of `da3dc42` (`CI` 42558804172, job `lint-test`) failed one test:
    `test_deeply_nested_json_is_reported` expected `not valid JSON`, but the
    coverage-instrumented runner parsed the 100,000-deep document and then
    reported `snapshot manifest must be an object`. The decoder's nesting
    failure is a `RecursionError` whose threshold moves with the interpreter
    build and coverage instrumentation, so `strict_json_loads` now scans the
    decoded text and rejects container nesting beyond 32 before decoding. Two
    tests pin the contract: one names the depth ceiling and one proves brackets
    inside strings are content. Design decision D2 and the surprise above
    record the reasoning.
  - [x] (2026-11-09) EP-M3 milestone CodeRabbit CLI review. Local CodeRabbit
    CLI 0.9.3 reviewed the committed diff `60df24c..ad9a868` (26 files) as
    `review_completed` with 8 findings (2 major, 3 minor, 3 trivial). All were
    verified against the source and repaired. Major: launcher output was
    buffered and replayed, now mirrored live with `echo=True` and no replay,
    and the proof working directory is the repository root; setup and probes
    were unbounded, now bounded by the remaining installation budget with
    process-group termination on expiry. Minor: `--repo` became `--repo-root`
    for install/check/run, the snapshot group help no longer claims to run,
    and `install.repair` logs a bounded exception class name under the
    documented `kani.install.*` event vocabulary. Trivial: NumPy docstrings
    for the shared CLI helpers, assertion messages in the install tests, and
    the run test's cross-module private-helper import replaced by a shared
    fixtures module. All gates were re-run sequentially: `make fmt`,
    `make check-fmt`, `make lint`, `make typecheck`, `make test` (1075 passed,
    5 skipped), `make markdownlint` and `make nixie`.
  - [x] (2026-11-09) Remediated the hosted ExecPlan assessment and closed its
    four evidence gaps. Findings 1–8 were repaired in `304f0b1` (removals use
    Cuprum `UNSET` markers because `CommandSpec.env` is an overlay, so
    inherited `RUSTC`/`RUSTFLAGS`/Rustup selectors now really disappear),
    `bf19e6c` (the backend revision must equal the manifest's upstream
    commit), `a8c2a82` (links create their implicit parent directories),
    `66a2452` (transport stages privately, the 2 GiB payload and 64 MiB
    inventory ceilings are enforced while streaming, and attestation is
    bounded), `3fcf5fc` (installed backend trees and launcher bytes are
    compared with the inventory before activation), `6f65381` (toolchain
    component, metadata and extracted-byte digests are verified before any
    probe executes them) and `e873379` (snapshot proofs and probes follow the
    admitted Cargo route, refuse unprovable repository configuration and pin
    accepted profile backends to LLVM in the child). Evidence gaps were closed
    in `1bd1f86` (a bounded 64 KiB tail per stream via `CommandSpec.capture`
    and `on_line`, with `TailCapture` as a tested unit), `ec8dc80` (two
    identities reporting the same Kani version install into disjoint
    generations), `ee732f2` (Hypothesis-generated archive member graphs with
    an independent expected-tree oracle) and `143a968` (a two-identity
    rule-based state machine, two-process barrier, killed-owner and timeout
    tests, and seeded premature-pointer and post-setup-move negative
    controls). One fresh full `make all` after splitting the stateful tests
    into `tests/test_kani_snapshot_activation.py` passed every gate: 1143
    passed, 5 skipped, interrogate 100%, Pylint 10.00/10 and `ty` clean.
- [x] (2026-11-09) Remediated the candidate-bound EP-M3 re-assessment at
  `352b53f` (six findings, two evidence gaps). Installation now runs the same
  runtime-file contract as `check` before activation; `run` executes the
  generation it validated instead of re-reading the pointer; the manifest
  requires a non-empty, duplicate-free library list and verification requires
  two-way shared-object coverage; the identity probe pins the verified
  toolchain's `LD_LIBRARY_PATH`; `pyroot` is validated as a contained setup
  addition; toolchain identifiers are revalidated as one contained name; and
  the installation deadline reaches copying, hashing, extraction, tree
  validation and toolchain verification. New tests cover each boundary plus a
  pointer switch during `run`. `tests/test_kani_snapshot_interruption.py` adds
  two-process evidence for a killed staging installer and a seeded setup fault
  with a waiting peer. The full sequential gate stack (`fmt`, `check-fmt`,
  `lint`, `typecheck`, `test`, `markdownlint`, `nixie`) then passed on this
  candidate: 1163 passed, 5 skipped, interrogate 100%, Pylint 10.00/10, `ty`
  clean and every Mermaid diagram valid.
- [x] (2026-11-09) Remediated the second candidate-bound EP-M3 re-assessment at
  `5778e50` (four findings, two evidence upgrades). (1) `verify_toolchain` now
  requires the private rustup home itself to resolve inside the generation, not
  merely to contain the toolchain; installation and reuse both refuse an
  escaping home. (2) The new `python_root` module records every member below
  `pyroot` in a generation-local canonical `pyroot.json` and requires an exact
  match on reuse, closing the open-ended setup allowance with precise
  missing/extra/changed diagnostics. (3) Launcher placement copies in bounded
  chunks with a checkpoint and fsync per chunk, and `archive_read` wraps raw
  reads so header scans also checkpoint while skipping a large member's body.
  (4) Cache reuse rehashes the backend, inventory and launcher payloads instead
  of trusting recorded sizes, so an equal-size backend edit is refused.
  Evidence: the interruption suite now kills an installer after a partial
  staged write, proves the pointer unchanged, and repairs from the intact
  source release without restoring the damaged cache; the concurrency suite
  adds a two-actor Hypothesis lock model and a witness that stalls an owner
  inside setup so the contender's refusal is observed mid-pipeline, with the
  retry reusing the owner's generation. Every repair has a test that failed
  before it. The full sequential gate stack (`fmt`, `check-fmt`, `lint`,
  `typecheck`, `test`, `markdownlint`, `nixie`) passed on this candidate: 1175
  passed, 5 skipped, interrogate 100%, Pylint 10.00/10, `ty` clean, spelling
  and Mermaid validation clean.
- [x] (2026-11-10) Remediated the third candidate-bound EP-M3 re-assessment at
  `5a0dc88` (three findings, one evidence upgrade). (1) A `pyroot` link must
  now resolve inside the Python root itself, not merely inside the generation:
  the record stores link text, so a target in a sibling home would put
  unrecorded, mutable content on `PYTHONPATH`. (2) Blocking local work is
  supervised, not only checkpointed. `run_within` runs transport staging,
  payload verification, archive validation, extraction, Python-root recording,
  tree validation, toolchain verification, launcher placement and generation
  writes in a private daemon thread and abandons them at the remaining budget;
  this is safe because those writes are private staging or unreferenced
  generations and the active-pointer rename follows an explicit
  `pointer publication` budget check. (3) `check` and `run` resolve and
  validate the pointer while holding the same bounded identity lock as
  installation, so a reader cannot validate a cache a repair is rewriting. For
  this candidate a post-rename directory flush that timed out was logged as
  deferred durability; the fourth re-assessment rejected that contract, and the
  paragraph below records the replacement. The sixth re-assessment later
  replaced the rename-based pointer entirely with ordered, create-only
  activation records; the design and Progress record the final protocol. New
  tests cover a link into a sibling home, a retargeted contained link, a write
  stalled in `fsync` under a real short budget, an expiry immediately before
  the pointer rename with no temporary left behind, a barrier-controlled
  check-versus-repair refusal, and a repair forced by a transient probe failure
  and killed mid-staging where the previous generation still validates with
  healthy hooks before recovery. The added code pushed four gated modules past
  Pylint's 400-line ceiling, so pointer publication, generation-local writes
  and the bounded copy rule were extracted into `activation`,
  `install_generation` and `install_copy`, and the spawn-process targets into
  `tests/kani_snapshot_actors.py`, instead of suppressing the check per file.
  The full sequential gate stack passed on the working tree: 1181 passed, 5
  skipped, interrogate 100%, Pylint 10.00/10, `ty` clean, spelling, Markdown
  and Mermaid validation clean.
- [x] (2026-11-10) Remediated the fourth candidate-bound EP-M3 re-assessment at
  `8d7d0e8` (two findings). (1) The deadline now bounds every blocking
  operation on the install path that can be bounded safely. The pointer and
  cached-manifest reads on the reuse path are supervised, because a hung read
  would otherwise hold the identity lock past the deadline and abandoning a
  read mutates nothing. Staged payloads are published on the calling thread
  with a budget check before the directory creation and before every
  replacement: publication replaces shared files one at a time, so a supervised
  publisher that timed out could still replace a file after the installation
  had reported failure. The active-pointer rename stays a single synchronous
  atomic operation issued only while the budget remains. (2) Durability is part
  of activation again. The post-rename directory flush is bounded by the
  remaining budget and a timeout or `OSError` raises a distinct error naming
  the generation that is already visible, instead of logging deferred
  durability and returning success, or reporting a publication failure after
  the pointer had changed. New tests cover an expiry before payload
  publication, a stalled manifest read under a real short budget, a failed
  post-rename flush, and a flush timeout; each asserts the pointer state the
  error describes.
- [x] (2026-11-10) Remediated the fifth candidate-bound EP-M3 re-assessment at
  `cf14fbc` (three findings). (1) Publication now has a bounded, reconcilable
  protocol instead of relying on synchronous calls no deadline can interrupt. A
  durable `.publication-pending` marker in the identity directory precedes the
  first payload replacement and is removed only after every replacement and its
  directory flush are durable; `check` refuses a cache that still carries it and
  `install` repairs it. Every publication step is supervised, and a
  replacement that outlives the budget reports an unknown outcome, because the
  abandoned worker may still complete that atomic rename. The active-pointer
  rename is supervised the same way, and a timeout leaves its temporary
  document for a possible late completion rather than claiming either state.
  (2) Reuse reissues the identity-directory flush before reporting success, so
  a retry after an unconfirmed activation cannot report reuse until a flush
  succeeds. (3) The generation directory entry, the cached payload entries and
  their parent directory entries are flushed before the pointer moves. New
  tests cover a stalled replacement under a real short budget, a cache refused
  while its publication is unfinished, an install that fails its payload or
  generation flush without activating, and a retry that cannot reuse an
  activation whose flush failed.
- [x] (2026-11-10) Remediated the sixth candidate-bound EP-M3 re-assessment at
  `e81bafb` (four findings). (1) Activation is a create-only write into an
  identity-level directory of ordered records instead of a rename onto one
  mutable file. A record is written privately, fsynced and hard-linked to the
  next free sequence number; an abandoned worker holds the number it observed,
  so it can only fail against a number a later activation already took or add a
  lower-ranked record, never replace or outrank the newer one. Records below
  the newest are pruned after the new record is durable. (2) Each publication
  owns a uniquely named marker, made durable before the first replacement and
  removed only by its own completion, so an abandoned completion cannot clear a
  later repair's marker; stale markers are cleared only once the new marker is
  durable. (3) Activation now flushes the nested directories holding the
  required runtime paths and the setup-created executables, and applies
  launcher modes before the fsync that makes its bytes durable, so a crash
  cannot leave a durable activation naming an unusable launcher. Reuse also
  reissues both activation-directory flushes. (4) Staging-directory creation
  and removal are supervised; an abandoned creation is private and uniquely
  named, and an abandoned removal touches only this run's directory. New tests
  cover a barrier-controlled activation that times out, completes another
  activation and is then released, the overlapping timeout/repair/interrupted
  publication sequence, launcher mode-before-fsync ordering, a failed launcher
  directory sweep, and stalled staging creation and cleanup under a real short
  budget.
- [x] (2026-11-10) Remediated the seventh candidate-bound EP-M3 re-assessment at
  `8cbd68c` (three findings and one plan-consistency gap). (1) Activation now
  flushes every entry its runtime contract requires: the compiler, the
  toolchain component record and every recorded library, the `bin` and `lib`
  directories plus each library's ancestor chain, and every file and directory
  of the setup-created Python root, files before their directories and
  directories deepest first. Flushing only the executables the record named
  left the libraries the compiler loads reachable through unflushed directory
  entries. (2) Blocking work that may safely be abandoned is now supervised:
  the staged manifest read, and every removal of private, uniquely named state
  such as a pending activation document, a partially copied launcher or
  generation file, and a staging tree. Those removals are deferred and
  debug-logged when they outlive the budget, because no reader resolves their
  names, and a single `cleanup` helper owns that distinction. (3) A stale
  publication marker that cannot be removed now fails the installation instead
  of being logged, because `check` refuses any marked cache and continuing
  would report a usable generation the cache cannot serve. (4) This plan's
  remaining `active.json` wording is reconciled with the ordered create-only
  record protocol. New tests cover a recorded library flushed before its
  ancestor directories, a Python-root member whose failed flush prevents
  activation, a stalled staged-manifest read under a real short budget, a
  stalled pending-record cleanup that is deferred while the activation still
  publishes, and a stale marker whose failed removal fails the repair and leaves
  `check` refusing the cache.
- [x] (2026-11-10) Remediated the eighth candidate-bound EP-M3 re-assessment at
  `db50eca` (two findings and one plan-consistency gap). (1) The pre-activation
  sweep now covers every file and directory the backend inventory publishes, so
  a setup-extracted member such as `share/data.txt` and the directory entries
  naming it are durable like a named binary. Each required toolchain file's
  whole ancestor chain is flushed as well, so the component record's
  `lib/rustlib` directory is required even when no recorded library sits
  beneath it. (2) The installation deadline now bounds the generation
  directory's creation, the cached-inventory read that feeds the sweep, and the
  activation-record listing that allocates the next sequence number. All are
  supervised under a protocol whose abandoned work is safe: the creation is
  private and idempotent, and the reads only observe. (3) The D4 departure and
  its successive refinements are recorded as explicitly accepted in the
  decision log, as the conformance basis requires. New tests cover a backend
  inventory member flushed before its directory and refusing activation when
  its flush fails, the component directory flushed and fatal when it cannot be
  flushed without a recorded library beneath it, and stalled generation
  creation and record enumeration under a real short budget.
- [x] (2026-11-10) Remediated the ninth candidate-bound EP-M3 re-assessment at
  `4210283` (two findings). (1) The overall deadline now bounds the work that
  ran before the identity lock and the runtime-file check that ran while
  holding it: creating the identity directory, opening the lock file and the
  four runtime-file stats are supervised, and the lock wait is bounded by the
  seconds the installation still has rather than only by its configured
  timeout. (2) A publication marker now records the activation sequence it
  follows, and readers ignore a marker that a later activation superseded.
  Because reuse reissues the record it validated, a marker that an abandoned
  publication writes after a later successful install ranks below that record
  instead of making a completed cache look unfinished; reuse then prunes the
  markers its record superseded. A marker from an older installation, or one
  whose content cannot be read, still counts as pending. New tests cover a
  stalled identity-directory creation, a lock wait that spends only the
  remaining budget, a stalled runtime-file check, marker classification by
  activation sequence, and a barrier-controlled abandoned marker write released
  after a later reuse.
- [x] (2026-11-10) Remediated the tenth candidate-bound EP-M3 re-assessment at
  `66ab5ee` (three findings). (1) Ordering markers against the activation
  record could retire the marker of an unfinished repair when an unrelated late
  activation landed, so a marker now records the identity's completion
  watermark: one small file advanced only by a lock-holding installation after
  its activation record is confirmed durable. Only a completion that follows a
  marker retires it, so a late activation is irrelevant while a late marker
  write from an abandoned publication still cannot make a completed cache look
  unfinished. (2) Publication no longer reads the activation record at all, so
  a corrupt newest record can no longer make every repair fail; the ordering
  comes from the watermark, and a reinstall publishes a new record above the
  damaged one. (3) Four reads that ran unbounded while the identity lock was
  held are now supervised: the asset-directory probe, checksum verification,
  the publication marker listing and the activation-directory probe. `check`
  also supervises the publication-state, active-pointer and manifest reads it
  performs under the lock. New tests cover the late-activation case, a
  corrupt-record repair, stalled asset-directory, checksum, marker-listing and
  activation-directory reads, and the three stalled reads `check` performs.
- [x] (2026-11-10) Remediated the eleventh candidate-bound EP-M3 re-assessment
  at `2045556` (one finding). A completion write that the deadline abandoned
  and a later worker resumed could raise the watermark above the value a repair
  read before writing its marker, retiring the repair's marker although no
  installation had finished since it began. A marker now records its base: the
  highest activation record the publication observed, read from record names
  only. It stays pending until the completion watermark rises strictly above
  that base. Because an installation links its record above every existing
  record before recording completion, a completion above a base always belongs
  to an installation that started after the publication, while a completion
  whose record the publication already saw ranks at or below the base however
  late its write lands. The activation read that supplies the base is
  supervised as `activation sequence read`. New tests cover the
  barrier-controlled sequence of the finding and a stalled base read under a
  real short budget.
- [x] (2026-11-10) Remediated the twelfth candidate-bound EP-M3 re-assessment
  at `0574a88` (one finding). A completion write abandoned at the deadline and
  resumed after a later installation had recorded its own could replace the
  watermark with the older sequence, so a marker the later completion had
  already retired looked pending again and `check` refused an intact cache.
  Completion evidence is now one create-only record per finished installation,
  named by the sequence it completed, so the watermark is their highest value
  and a late write joins the order beneath a newer record instead of replacing
  it. Older records are pruned after the new one is durable, and a deferred
  prune is a harmless leftover. A barrier test reproduces the sequence and
  requires `check` to accept the cache after both abandoned workers resume.
- [x] (2026-11-10) Remediated the thirteenth candidate-bound EP-M3
  re-assessment at `b9a6d72` (one finding). The watermark was computed from
  directory entry names alone, so any entry whose name looked like a completion
  record could retire a pending marker: a short or non-canonical sequence, a
  directory or symlink, or a record whose contents named another sequence.
  Completion evidence now counts only when its name is canonical, its entry is
  a regular file, and its contents name the same sequence; anything else is
  ignored, which keeps markers pending and makes `check` refuse the cache. A
  test seeds every malformed shape above the real completion beside an
  unfinished marker and requires the watermark not to move and `check` to
  refuse the cache.
- [x] (2026-11-10) Remediated the fourteenth candidate-bound EP-M3
  re-assessment at `4d2e4f0` (one finding). A canonically named completion
  record was read in full before its contents were compared, so an oversized
  record could exhaust the process deciding whether a marker is pending. The
  read now stops one byte past a canonical record, so an oversized record is
  refused without being loaded. A test seeds a 1 MiB canonical record beside an
  unfinished marker, records every read size through a wrapped handle, requires
  each read to carry a bound, and requires the marker to stay pending and
  `check` to refuse the cache.
- [x] (2026-11-10) Remediated the fifteenth candidate-bound EP-M3 re-assessment
  at `7f2c0c3` (one finding). The marker read was unbounded, and a marker base
  of more than 4,300 digits made `int()` raise `ValueError`, which escaped both
  `check` and the reuse path an installation needs to repair the cache. The
  marker read now stops one byte past the longest canonical marker, and a base
  longer than an activation sequence is unparsable, so it names no base and
  stays pending instead of raising. A regression writes a 5,000-digit marker
  base, requires `marker_base_sequence` to return `None`, requires `check` to
  refuse the cache, then requires a subsequent install to clear the stale
  marker and `check` to pass.
- [x] (2026-11-10) Remediated the sixteenth candidate-bound EP-M3
  re-assessment at `5988765` (one finding). The bounded marker read stripped
  trailing whitespace before parsing, so a marker whose base was followed by
  padding that filled the read, and an invalid suffix beyond it, parsed as the
  visible base. A marker naming base zero is below every completion watermark,
  so `check` could accept a cache whose publication had not finished. The read
  now refuses a marker longer than the longest canonical one, requires the
  terminator `render_marker` writes, and parses only the complete remainder, so
  a truncated or padded marker names no base and stays pending. A regression
  writes a valid-looking base, twenty spaces and a trailing byte, requires
  `marker_base_sequence` to return `None`, requires `check` to refuse the
  cache, then requires a subsequent install to clear the marker and `check` to
  pass.
- [x] (2026-11-10) Remediated the seventeenth candidate-bound EP-M3
  re-assessment at `5bde0fd` (one finding). A complete marker was trusted for
  its parsed base alone, so a leading zero, which `render_marker` never writes,
  still named a base below the completion watermark and could retire itself.
  The parser now accepts a base only when the whole file is exactly the bytes
  `render_marker` writes for it, so any other spelling names no base and stays
  pending. A regression requires the canonical base zero to keep parsing,
  requires `publishing base=00` to name no base, requires `check` to refuse the
  cache, then requires a subsequent install to clear the marker and `check` to
  pass.
- [x] (2026-11-10) Remediated the eighteenth candidate-bound re-assessment at
  `47cb5ea` (one finding, plus the verification obligations it named). The
  attestation boundary trusted the verifier's exit status and result shape but
  never compared the verified statement with the manifest's workflow identity,
  and `stage_assets` discarded the returned records. `verify_attestation` now
  takes one `AttestationRequest` — policy, artefact, bundle and the manifest's
  `WorkflowIdentity` — and requires at least one verified record's
  `verificationResult.statement.predicate.runDetails.metadata.invocationId` to
  equal
  `https://github.com/<repository>/actions/runs/<run_id>/attempts/<run_attempt>`
  built from the policy repository and that identity, so an attestation for a
  different run, or for another attempt of the same run, authenticates nothing.
  The request travels through `AttestationVerifier` and `InstallHooks`, the
  transport builds it from `parsed.manifest.workflow`, and the shared fixture
  double refuses any other identity. The attestation boundary now lives in its
  own `attestation.py`, with `integrity.py` keeping manifest, payload and
  checksum verification, so the added correlation does not push a module past
  the configured size limit. New tests cover a matching run, a wrong run, a
  wrong attempt, and records whose statement or invocation ID is absent or
  malformed, and the behavioural `gh` stand-in now reports the manifest's own
  run, so the CLI scenarios exercise the correlation end to end. The seeded
  authentication-bypass negative control required by V2 is added: with the
  manifest digest comparison replaced by a no-op, a release rewritten after
  review reaches the setup spy, which shows the companion refusal is caused by
  that comparison rather than by an unreachable spy.
- [x] (2026-11-10) Closed the nineteenth candidate-bound re-assessment's V2
  evidence gap. The run correlation was accepted, but the verification plan's
  wrong-signer, wrong-source and wrong-ref cases were covered only by
  policy-flag assertions and a generic verifier failure. In
  `test_kani_snapshot_integrity.py` a double now enforces every policy flag the
  way `gh attestation verify` does; a parametrized test changes one of signer
  workflow, source ref, source digest or signer digest and requires the refusal
  to surface as `CommandFailedError`, while the trusted policy passes through
  the same double, so the check is not vacuous. In
  `test_kani_snapshot_install.py` the real `verify_attestation` runs against a
  modelled bundle that fails one dimension at a time, and installation must
  stop before the setup spy, which is the extraction boundary. Real signature
  verification remains EP-M5's boundary. Stale duplicated checksum constants
  and an out-of-date parameter list in `attestation_command`'s docstring were
  removed in the same change.
- [x] (2026-11-10) Closed the twentieth candidate-bound re-assessment's V2
  evidence gap. The previous verifier double compared the command's flags with
  a policy the test itself supplied, and the installation double always
  refused, so neither modelled a bundle identity. A shared
  `tests/kani_snapshot_attestation_fixtures.py` now models
  `gh attestation verify`: it holds the identity a bundle claims, reads the
  trusted flags from the argument vector the production code built, refuses the
  first dimension where the two disagree, and accepts a matching bundle.
  Parametrized tests require a wrong signer workflow, source ref, source digest
  and signer digest to be refused, require a command that requests an untrusted
  identity to be refused even when the bundle claims that same identity, and
  require a matching bundle to pass, so the model is not vacuous. The
  installation test now runs the real `verify_attestation` against a modelled
  bundle that fails one dimension at a time and spies on launcher extraction as
  well as setup, because the installer extracts the launcher before calling
  setup; both spies must stay empty for every refusal, and a matching-bundle
  control requires both to be reached. Real signature verification remains
  EP-M5's boundary.
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

Hosted `lint` runs `interrogate` under Python 3.13 even though the project
targets Python 3.14, because the tool's Python is whatever the runner's `uv`
resolves rather than a pinned interpreter. `ruff format` with
`target-version = "py314"` rewrites a parenthesized multiple-exception clause
without an `as` binding to the parenthesis-free PEP 758 form, which Python 3.13
cannot parse, so the local `make lint` passed while the hosted run failed with
`SyntaxError: multiple exception types must be parenthesized`. Source must stay
parseable by 3.13 until the tool's interpreter is pinned; separate failure
modes instead of relying on the parenthesis-free form.

Repeating a local install does not reread `--asset-dir`: the reuse check runs
under the identity lock before staging, so a healthy active generation is
returned even when the local release directory has changed. The changed-assets
refusal therefore belongs to a repair attempt and needs an invalid active
generation first; the behavioural scenario corrupts the receipt to reach that
path. This is intended cache behaviour, not a missing integrity check.

The local `--asset-dir` requirement is a deliberate plateau. The D4 release
host policy and downloader are already implemented and tested, but the consumer
command does not yet fetch release assets from GitHub. EP-M4 must connect that
transport before the workflow can depend on this command, and the current error
message says so explicitly.

A hostile-nesting test that passes locally can fail in hosted CI without any
code change. `json`'s depth failure arrives as `RecursionError`, whose
threshold depends on the interpreter build and on coverage instrumentation; the
hosted runner under `slipcover` parsed a 100,000-deep document that the local
interpreter refused, so the assertion saw a later "must be an object" error.
The strict decoder now owns a fixed container-nesting ceiling, which makes the
outcome independent of the runner.

`CommandSpec.env` is an overlay, not a replacement. Deleting a key from a
copied mapping removes nothing from the child because the host value is still
inherited. Removing a selector needs an explicit Cuprum `UNSET` marker, which
is why an earlier implementation silently kept `RUSTC`, `RUSTFLAGS` and the
Rustup distribution selectors in snapshot setup, probe and proof children.

Hypothesis `@precondition` predicates receive only the state machine, never a
rule's drawn arguments. A predicate written as `lambda self, identity: ...`
fails at initialization. Use an in-body early-return guard when the decision
depends on a drawn argument. Function- scoped fixtures also persist across
generated examples, so a generated test that needs a fresh directory must
create one inside the test.

Killing a child process while it waits in `multiprocessing.Event.wait` leaves
the shared condition variable's sleeper accounting inconsistent, and the
parent's next `set()` can block indefinitely. A kill-target worker uses
`time.sleep` instead of an event wait.

The case that reaches repair in a process test is the identity's cached copy,
not the source release. Damaging `--asset-dir` leaves reuse working because the
active generation is validated against the private downloads staged earlier;
corrupting the cached backend is what forces a new staging attempt while the
old pointer still names the previous generation.

The backend inventory cannot describe setup-created content, so an allowed
addition such as `pyroot` must be treated as a subtree rather than one entry.
Exempting only the directory name made every real Python payload an "unrecorded
member" failure; exempting its descendants and validating the root and every
link separately keeps the inventory exhaustive for backend content without
rejecting what setup is documented to add.

Digest-checking only recorded libraries cannot prove coverage: the proof and
probe put `<toolchain>/lib` first on `LD_LIBRARY_PATH`, so any unrecorded
shared object there is loadable. Verification therefore walks that directory,
follows only contained links and rejects every shared object the manifest omits.

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

2026-11-09, implemented D1/D2: strict version 1 parsers reject unknown and
duplicate keys, boolean-as-integer coercion, non-finite numbers, over-limit
integers, hostile nesting, malformed SHAs/tags and unsupported targets. Asset
names must be printable basenames and may not shadow `manifest.json` or
`SHA256SUMS`, because consumers build local paths from those names. The cache
key covers every identity field in canonical JSON, so a shared reported Kani
version cannot alias two snapshots.

2026-11-09, implemented D4 setup boundary: installation passes only verified
archive bytes to the upstream launcher, sets `KANI_HOME`, `RUSTUP_HOME` and
`CARGO_HOME` inside the generation, removes inherited mirror/selection
variables, and re-validates the setup-created tree and compiler identities
before activation. `check`/`run` never invoke setup and set
`RUSTUP_AUTO_INSTALL=0` so a missing toolchain fails without network repair.

2026-11-09, implemented D4 archive safety: the archive reader owns a stricter
policy than `tarfile`'s extraction filters. It validates the whole member graph
before writing, treats PAX/GNU sparse metadata and privileged mode bits as
hostile, strips exactly the configured bundle root, and refuses any symbolic
link that escapes, dangles, cycles or exceeds 16 hops. Extraction never merges
with existing content, writes only through `O_EXCL|O_NOFOLLOW` descriptors,
creates links only after files, and publishes a staging tree by atomic rename.

2026-11-09, implemented D4 download policy: snapshot assets bypass the generic
downloader's acceptance of arbitrary HTTPS hosts. The initial request must be
the exact release URL derived from the reviewed pin, redirects are restricted
by host and path before they are opened, and only transient transport failures
are retried. A redirect or integrity failure is never retried.

2026-11-09, implemented D4 inventory binding: the manifest's separately hashed
`inventory.json` is parsed against one exact version 1 shape and consumed by an
exhaustive tree comparison. The inventory is never trusted to be a subset: an
unrecorded member, a special file or a replaced hard link fails the generation,
so a cache hit is evidence about content rather than only about directory
existence. Modes are ordinary permission bits only, matching the archive policy.

2026-11-09, implemented D4 execution isolation: identity directories are keyed
by the complete pin, generations are uniquely named at their permanent
location, and proofs receive generation-private Kani/Rustup/Cargo homes with
the exact toolchain selected. Inherited `RUSTC`, `RUSTFLAGS` and Rust
distribution overrides are removed; admission wrappers and `RUSTDOC` are kept.
The forwarded-argument refusal list is finite and evidence-based: upstream's
proxy accepts only `setup` with `--use-local-bundle`/`--use-local-toolchain`,
and pinned driver source exposes `-Z lean` as the backend switch.

2026-11-09, implemented D4 local transport: `install` requires `--asset-dir`
until the release downloader is connected. The transport is explicit so a
consumer cannot silently fall back to a host Kani, and every transport file is
bounded and copied into a private downloads directory before verification.
Repeating an installation validates and reuses the active generation before
reading transport files, so changed local assets cannot disturb a healthy cache.

2026-11-09, implemented D4 archive/inventory ordering: the manifest pins the
archive bytes, so structural agreement runs before setup and setup consumes the
same verified archive. `validate_archive_inventory` requires two-way path
equality plus equal kind, mode, size and link target. Full payload digests are
rechecked at stage time; cache reuse rechecks sizes for every asset and full
digests for the inventory and launcher, then re-reads the backend archive
against the inventory.

2026-11-09, implemented D4 launcher staging and run revalidation: the launcher
archive is extracted privately and its binaries are copied beside the backend
after setup without overwriting backend files. `check` and `run` never install;
they reload the cached manifest, receipt, payload evidence, absolute toolchain
link and live probes, and any failure is reported without mutating the
generation.

2026-11-09, implemented D4 activation state: the per-identity advisory lock
uses non-blocking `flock` with a bounded poll inside the installation deadline.
The active pointer is a strict two-key document written to a private temporary
file, fsynced, renamed and followed by a directory fsync. A corrupt active
generation triggers repair into a new generation, and the old generation
directory is never deleted.

2026-11-09, implemented D4 review remedies: `run` now sets `echo=True` on the
Cuprum command so the launcher's stdout and stderr are mirrored live while
remaining captured, and the CLI no longer replays the captured copy. The proof
working directory is the repository root, as D1 specifies. Setup and the
identity probes receive the remaining installation budget; a bounded command
runs in its own process group so expiry terminates descendants instead of
merely checking the clock before and after the call. Snapshot commands expose
`--repo-root` to match D1 and the legacy commands, and event names follow the
documented `kani.install.*` vocabulary with bounded fields.

2026-11-09, implemented D4 budget remedies: the two identity probe commands
share one budget, which is recomputed before each command and rejected once
exhausted. Cache revalidation measures the probe budget through a callable
immediately before the probe, and reuse is reported only after a final deadline
check, so slow checks or a slow first command cannot silently extend the
deadline. `run_command` resolves an enclosing Cuprum scope's default timeout
before choosing process-group ownership, because that timeout also terminates
the command, and reports the effective timeout from `TimeoutExpired` rather
than formatting an absent per-spec value.

2026-11-09, implemented D2 nesting ceiling: `strict_json_loads` rejects
container nesting beyond 32 before decoding, counting brackets outside JSON
strings. The ceiling is part of the document contract rather than an
interpreter side effect, so hostile input fails identically on local,
coverage-instrumented and hosted runners.

2026-11-09, implemented D4 environment removal: `CommandSpec.env_mode` is
`OVERLAY` by default, and removing an inherited variable requires a Cuprum
`UNSET` value rather than omitting or deleting a key from a copied mapping.
Snapshot setup, probes and proof children unset `RUSTC`, `RUSTFLAGS` in both
spellings, the Rustup distribution selectors and `TAR_OPTIONS`, while keeping
`RUSTC_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER` and `RUSTDOC` so admission still
applies. Tests inspect the real child environment and confirm the parent's
environment is unchanged.

2026-11-09, implemented D4 toolchain provenance: the manifest's toolchain group
records each Rustup component archive's name, version, HTTPS URL, length and
digest, the channel metadata digest, the extracted `rustc` digest, the
`lib/rustlib/components` digest and the digest of every extracted compiler or
runtime library at a contained relative path. `verify_toolchain` compares the
installed tree with those records on both the fresh-install and cache-reuse
paths before the compiler probe, so a probe can no longer establish only that
some compiler calls itself the expected version.

2026-11-09, implemented D4 installed-tree verification: validation compares the
installed `kani-<version>` tree with the published inventory, allowing only the
documented setup additions, and compares each placed launcher file with the
digest-pinned launcher archive. Install runs the same check before writing the
active pointer, so tampered or lost installed bytes are never executed after a
supposedly successful install.

2026-11-09, implemented D4 admitted execution route: snapshot probes and proofs
use the same Cargo route as the qualified release path. The configured
`RUSTC_WRAPPER`/`RUSTC_WORKSPACE_WRAPPER` chain runs every probed executable,
repository configuration naming an unprovable route is refused, accepted
profile backends are pinned to LLVM in the child mapping only, inherited
compiler flags are sanitized into both `RUSTFLAGS` spellings, and
`LD_LIBRARY_PATH` selects the pinned toolchain's libraries. The shared helpers
live in `kani_exec_routing` so the release and snapshot paths cannot drift.

2026-11-09, implemented D4 bounded proof output: `CommandSpec` gains `capture`
and an `on_line` hook, and `run_snapshot` disables capture while `TailCapture`
retains the newest 64 KiB of each stream. Mirrored output is never replayed and
the launcher's exit status is preserved, so one verbose proof cannot size the
CLI's memory.

2026-11-09, implemented V3/V4 evidence shape: generated archive graphs are
checked against an independent expected-tree oracle and complement the fixed
hostile fixtures, while a two-identity rule-based state machine drives the real
install pipeline through failure, corruption and repair. Seeded premature
pointer writes and post-setup directory moves are negative controls that must
fail validation; POSIX locking and rename durability stay axioms tested at the
process boundary.

2026-11-09, implemented D4 activation predicate: a fresh installation now runs
the runtime-file contract that `check` enforces, before the probe executes the
binaries, and `run` executes the exact generation returned by validation rather
than re-reading the active pointer. Publishing or launching an unvalidated
generation is therefore impossible on the install path and unreachable on the
execution path even while a concurrent repair switches the pointer.

2026-11-09, implemented D4 toolchain containment: the manifest's toolchain
identifier is one plain name and its resolved path must stay inside the
generation's private Rustup home. Lifecycle verification requires two-way
shared-object coverage under `<toolchain>/lib`, and the identity probe applies
the same pinned `LD_LIBRARY_PATH` as the proof so the probe observes the
libraries execution will load.

2026-11-09, implemented D4 setup-addition validation: `pyroot` is exempt from
inventory matching as a subtree because setup creates it, but must be a real
directory inside the generation whose links all resolve inside it. Inventory
exhaustiveness continues to cover all backend content, while setup-created
content is contained rather than merely ignored.

2026-11-09, implemented D4 mid-phase deadlines: the installation budget
provides checkpoints threaded through transport copying, digest hashing,
archive extraction and structure validation, launcher placement, installed-tree
validation and toolchain verification. Each long local phase stops at expiry
instead of only being checked before it starts and after it ends.

2026-11-09, implemented D4 Python-root record: the inventory's `pyroot`
exemption is closed by a generation-local `pyroot.json` written after setup. It
records every member below the root with path, kind, mode, size, digest and
contained link target, and reuse requires byte-identical content. Recording
rather than extending the manifest keeps the producer from having to publish
setup's output, and it makes setup nondeterminism and post-install drift
detectable instead of importable.

2026-11-09, implemented D4 reuse and interruption hardening: the private rustup
home must resolve inside its generation; cache reuse rehashes the backend,
inventory and launcher payloads and re-reads each archive's structure instead
of trusting recorded sizes; launcher placement copies in bounded, fsynced
chunks and archive scans checkpoint raw reads, so both stop inside a large
transfer. Evidence adds a two-actor Hypothesis lock model over the real
`flock`, a witness that stalls an owner inside setup so the contender is
refused mid-pipeline and later reuses the owner's generation, and an
interruption test that repairs from the intact source release after a killed
partial staged write without restoring the damaged cache.

2026-11-10, implemented D4 supervised work and reader locking: cooperative
checkpoints cannot interrupt a `read`, `write` or `fsync` stalled on a hung
filesystem, so `progress.run_within` runs each byte-moving local operation in a
private daemon thread and the caller waits no longer than the remaining budget.
Wrapping is safe only where the output is a private staging directory or an
unreferenced generation; every publication stays on the calling thread and the
active-pointer rename is preceded by a `pointer publication` budget check after
the temporary document is durable, so an abandoned worker can never make shared
state visible. `check` and `run` now hold the bounded identity lock while
resolving and validating the pointer, matching the design's reader rule,
because a repair publishes verified payloads file by file under that lock.
`pyroot` links must resolve inside the Python root because the record describes
link text, not reachable content; a target in a sibling generation home would
be neither recorded nor observed on reuse.

2026-11-10, revised D4 bounded publication and reconciliation: the fourth
re-assessment showed that reuse still read the pointer and manifest
unsupervised, that staged payloads were published without a budget check, and
that a post-rename flush failure or expiry was logged as deferred durability
while install reported success. Reuse reads are now supervised, because
abandoning a read mutates nothing. Activation includes durability: the
post-rename directory flush is bounded by the remaining budget, and a timeout or
`OSError` raises an error naming the generation that is already visible. The
fifth re-assessment then showed that synchronous publication cannot keep the
deadline, that reuse did not reissue an unconfirmed flush, and that the
generation and payload directory entries were never flushed. Publication now
writes a durable `.publication-pending` marker before the first replacement and
supervises every step, so an unfinished publication is visible to readers and
repaired by the next install; a replacement or rename that outlives the budget
is reported as an unknown outcome rather than as success or failure. Reuse
reissues the identity-directory flush before reporting success, and the
generation and payload parent entries are flushed before activation. Success
therefore still means the activation was flushed, while a failure distinguishes
visible-but-not-durable activation from a publication that never happened.

2026-11-10, revised D4 ordered activation and per-publication markers: the
sixth re-assessment showed that a supervised rename can be abandoned and later
overwrite a newer activation, that a shared marker name lets an abandoned
completion remove a repair's marker, that activation did not flush nested
generation entries or setup-created executables, and that staging creation and
cleanup ran unbounded. Activation is now a create-only hard link into an
ordered record directory: the highest sequence wins, an abandoned worker holds
the number it observed, and a later activation takes that number first, so the
abandoned link can only fail. Each publication owns a marker named for it, and
completion removes only that marker, so overlapping publications cannot clear
each other's evidence. The installer flushes the nested runtime directories and
the setup-created executables, and applies launcher modes before the fsync that
makes the bytes durable. Staging creation and removal are supervised because
both touch only this run's private, uniquely named directory. A supervised
shared write is now allowed only where its published name is unique or totally
ordered; every other shared mutation stays on the calling thread or behind an
explicit budget check.

2026-11-10, revised D4 durability coverage and cleanup ownership: the seventh
re-assessment showed that activation flushed only the executables the record
named, not the toolchain component record, the libraries the compiler loads,
the directories that make them reachable, or setup's Python tree; that the
staged manifest read and several private removals were unsupervised; and that a
stale publication marker whose removal failed was logged while the installation
reported success. A tree flush now writes back every regular file below a root
before the directories that hold it, and the generation sweep covers the
component record, every recorded library and each library's ancestor chain. The
staged manifest read and every removal of private, uniquely named state are
supervised, and a removal that outlives the budget is deferred and debug-logged
because no reader resolves its name. A stale marker whose removal fails is
fatal instead, because `check` refuses any marked cache, so continuing would
report a generation the cache cannot serve. A dedicated cleanup module owns the
private-removal contract so a later private removal cannot silently become
shared-state cleanup.

2026-11-10, confirmed D4 acceptance and revised its durability and bounded-work
coverage: the eighth re-assessment showed that the sweep covered only named
runtime entries and each recorded library's ancestors, leaving arbitrary
backend inventory members and the component record's `lib/rustlib` directory
unflushed, and that generation-directory creation and the activation-record
listing ran synchronously while holding the identity lock. The sweep now
flushes every backend inventory file and directory plus each required toolchain
file's whole ancestor chain, and the creation, cached-inventory read and record
listing are supervised under protocols whose late work is safe: the creation is
private and idempotent, and the reads only observe. The ordered create-only
activation record is design revision 4's D4 contract, which plan approval
ratifies. Its later durability, cleanup, publication and bounded-work
refinements, including this revision, are explicitly accepted here under the
user's implementing instruction of 2026-11-10, which directs this plan's
implementation to continue through the hosted assessment to equilibrium; this
entry is the recorded acceptance the conformance basis requires for that
departure.

2026-11-10, extended the bounded-work rule to the lock boundary and ordered
publication markers against activation: the ninth re-assessment showed that the
identity directory's creation, the lock file's open and the runtime-file stats
before the probe still ran synchronously, and that the lock wait was budgeted
from the configured timeout alone, so time already spent under the deadline
could be spent again. Those operations are now supervised, and the wait is
bounded by the remaining budget. It also showed that a marker whose write
outlived its budget could be published by the abandoned worker after a later
installation had completed, leaving a cache that `check` refused although the
installation had succeeded. A marker now records the activation sequence it
follows, and a marker below the active record is not pending. Completion always
advances that record — activation publishes the new generation and reuse
reissues the record it validated — so late marker work ranks below the
activation it lost to. A marker with no readable sequence stays pending,
because an unreadable marker is exactly the case in which the cache must not be
assumed complete. Reuse prunes the markers its record superseded; a deferred
prune is harmless because the leftover ranks below the active record. That
ordering was superseded by the completion watermark recorded in the next entry,
which removes the dependence on the activation sequence.

2026-11-10, ordered publication markers against a completion watermark rather
than the activation sequence: the tenth re-assessment showed that an unrelated
late activation can outrank the record a publication observed without that
publication having finished, so the marker of an unfinished repair could be
retired and `check` could accept a half-repaired cache. The activation order
therefore cannot carry the meaning "an installation finished after this marker
was written". A per-identity completion watermark now records exactly that: it
is written only by a lock-holding installation after its activation record is
confirmed durable, so no abandoned worker can advance it on its own, and a
marker is pending unless the watermark has moved past the value the marker
recorded. Publication reads the watermark instead of the record, which also
removes the repair path's dependence on parsing the newest record: a corrupt
record is replaced rather than fatal. A late watermark write names a sequence
the activation order already published, which was taken to keep markers pending
rather than retiring them. The eleventh assessment showed that inference was
wrong: a late value can exceed the watermark a later publication merely read,
so it can retire that publication's marker; the next entry replaces the
recorded value with the publication's base. The four shared reads that remained
unbounded under the lock — the asset-directory probe, checksum verification,
the publication marker listing and the activation-directory probe — are now
supervised as well.

2026-11-10, ordered publication markers by their base rather than by the
watermark they read: the eleventh re-assessment showed that a completion write
abandoned at the deadline and resumed later can raise the watermark above the
value a repair read before writing its marker, retiring the repair's marker
although no installation had finished since it began. A marker now records its
base — the highest activation record the publication observed, taken from
record names only — and stays pending until the completion watermark rises
strictly above that base. Because an installation links its record above every
existing record before it records completion, a completion above a base always
belongs to an installation that started after the publication, while a
completion whose record the publication already saw ranks at or below the base
however late its write lands. The earlier fixes are preserved: records are
still never parsed, so a corrupt newest record cannot block a repair, and an
unrelated activation that records no completion cannot raise the watermark. The
base read is supervised, and the watermark remains the only evidence that an
installation finished.

2026-11-10, recorded completion as one create-only record per installation
rather than one mutable watermark file: the twelfth re-assessment showed that
an abandoned completion write resuming after a later installation's own
completion could lower the watermark, so a marker the later completion had
already retired became pending again and `check` refused an intact cache.
Naming each record by the sequence it completed makes the watermark their
highest value; a late write adds evidence beneath a newer record instead of
replacing it, so the watermark can rise but never regress, and a marker is
still retired only by a completion above its base. Older records are pruned
after the new one is durable, and a deferred prune is a harmless leftover.

2026-11-10, required completion evidence to be canonical before it can retire a
marker: the thirteenth re-assessment showed that counting any directory entry
whose name looked like a completion record let a stray, malformed, wrongly
typed or content-mismatched entry raise the watermark and retire an unfinished
repair's marker. A record now counts only when its name is the documented
20-digit sequence, its entry is a regular file rather than a directory or a
symlink, and its contents name that same sequence. Anything else is ignored,
which keeps markers pending, so unaccountable evidence can only make readers
refuse the cache, never accept an incomplete one.

2026-11-10, bounded the completion-record read: the fourteenth re-assessment
showed that validating record contents still read the whole file first, so a
canonically named but oversized record could exhaust the process on the reader
path. The read now stops one byte past a canonical record and the comparison
happens against those bytes, so unaccountable evidence costs bounded memory as
well as failing to retire a marker.

2026-11-10, bounded the marker read and made an unparsable base an unknown
rather than an error: the fifteenth re-assessment showed that a marker carrying
more digits than Python converts raised `ValueError` out of `check` and out of
the reuse path, so an installation could not repair the cache it had refused.
The read now stops one byte past the longest canonical marker, and a base
longer than an activation sequence names no base, which keeps the marker
pending. Repair follows the ordinary path: a pending marker rejects reuse, and
the fresh publication's marker write clears the stale one once its own marker
is durable.

2026-11-10, refused incomplete markers instead of trusting their visible
prefix: the sixteenth re-assessment showed that stripping whitespace from the
bounded read let a marker whose padding filled the read, and whose suffix lay
beyond it, parse as the base its prefix named. A base that a later completion
may retire must name a complete marker, so the parser now rejects anything
longer than the longest canonical marker, requires the terminator
`render_marker` writes, and treats any deviation as no base. The marker then
stays pending, so the cache remains refused until an installation repairs it
through publication.

2026-11-10, accepted a marker base only from the canonical spelling: the
seventeenth re-assessment showed that requiring a complete marker still trusted
any digits that converted, so a corrupted `publishing base=10` written as
`publishing base=00` parsed as zero, which every completion watermark
supersedes. A marker now names a base only when the whole file is exactly the
bytes `render_marker` writes for that base, so a leading zero and every other
non-canonical spelling name no base. The canonical base zero is still accepted;
it is a spelling this module can write, even though a completion above it
retires the marker as before.

2026-11-10, correlated the verified attestation statement with the manifest's
workflow run: the eighteenth re-assessment showed that the verifier's exit
status only proves that some attestation satisfied the policy flags, so an
attestation for an unrelated run, or for a rerun of the same run, could
authenticate a manifest that records another attempt. `verify_attestation` now
requires a verified record whose SLSA v1 `runDetails.metadata.invocationId`
equals the run-and-attempt URL built from the policy repository and the
manifest's own `WorkflowIdentity`, and the identity is passed explicitly
through the hook and transport seam so no caller can omit it silently.
Predicate fields remain user-controllable, so this is a correlation the
verifier's signature already binds, not an independent observation: the
certificate pins signer identity and source digest, and this comparison closes
the remaining gap between "a trusted builder signed something" and "that
builder signed this run's statement".

2026-11-10, completed the V2 wrong-identity evidence: the nineteenth
re-assessment accepted the run correlation but observed that the verification
plan's wrong signer, source and ref cases were asserted only as policy flags
and a generic verifier failure. The verifier double now enforces every policy
flag the way `gh attestation verify` does, and a parametrized test proves each
wrong signer workflow, source ref, source digest and signer digest is refused
rather than masked, with the trusted policy accepted through the same double so
the check is not vacuous. A second parametrized installation test runs the real
`verify_attestation` against a modelled bundle that fails one dimension at a
time and proves setup is never reached, which is the wrong-signer, wrong-source
and wrong-ref refusal at the boundary where extraction would otherwise occur.
These modelled refusals are evidence about this code's propagation and
ordering, not about a real signature verification, which remains EP-M5's real
verifier boundary.

2026-11-10, modelled the bundle identity rather than the request: the twentieth
re-assessment observed that the verifier double was parameterized by the policy
under test, so it never compared trusted flags with a bundle, and that the
installation double always refused regardless of identity. The shared verifier
model now holds the identity a modelled bundle claims, compares it with the
flags the trusted command built, and accepts a matching identity, so the
wrong-signer, wrong-source and wrong-ref cases are refusals of a bundle rather
than of the request. Because the launcher is extracted before setup,
installation evidence now spies on extraction as well as setup and requires
both to remain uncalled for every refusal, with a matching-bundle control that
reaches both so the spies cannot pass vacuously.

2026-09-27, proposed D5/D6: attest build origin before clean validation, then
publish only with successful same-run proof evidence. Test public offline
transport before publication and actual public HTTP transport during bootstrap.
Neither a build attestation nor a zero-harness exit is proof success.

## Outcomes & retrospective

Planning review is complete. EP-M2 (authenticated pin and manifest contracts)
and EP-M3 (isolated install, check and run) are implemented, gated and through
their milestone CLI review. A hosted ExecPlan assessment of EP-M3 supplied
eight findings and four evidence gaps; all twelve were repaired or closed as
recorded in Progress, and the resulting tree passes the full gate with 1143
tests. Further candidate-bound re-assessments found forty-five more issues: the
runtime-file contract before activation, `run` executing the validated
generation, contained toolchain and Python-root records, rehashed payloads,
supervised blocking work with a pre-publication budget check, readers holding
the identity lock, supervised reuse reads, a durability contract that neither
claims an unflushed activation nor reports a publication failure after the
pointer changed, a durable publication marker with an explicit unknown-outcome
report for a supervised replacement, durability reasserted on reuse, generation
and payload parent-directory flushes before activation, ordered create-only
activation records, per-publication markers, nested generation-entry and
executable flushes with mode-before-fsync ordering, bounded staging creation
and cleanup, a durability sweep covering recorded libraries, the component
record and the Python root, supervised private cleanup, a stale publication
marker whose failed removal fails the installation, a backend-inventory
durability sweep with each required toolchain file's ancestor chain, supervised
generation creation and activation-record enumeration, bounded identity
directory and lock-file creation with a lock wait limited by the remaining
budget, publication markers ordered against a completion watermark rather than
the activation record, publication markers ordered by the base a later
completion must exceed, completion records that cannot regress when an
abandoned write lands last, completion evidence that must be canonical before
it can retire a marker and must not be loaded whole, repair that survives a
corrupt newest record, and bounded asset-directory, checksum, marker-listing,
activation-directory and activation-sequence probes, a bounded marker read that
accepts a base only from the canonical bytes `render_marker` writes, so an
unparsable base, an incomplete marker, a truncated tail or a non-canonical
spelling keeps the marker pending instead of raising, and attestation
verification that accepts a result only when a verified record names the
manifest's own run and attempt, and V2 wrong-signer, wrong-source and wrong-ref
refusal evidence at the verifier and installation boundaries, modelled as a
bundle identity that a matching command accepts and a wrong one is refused
before launcher extraction and setup. All are repaired with tests recorded in
Progress. EP-M1, EP-M4, EP-M5 and EP-O1, and all publication, have not started.
The consumer `install` command is deliberately limited to a verified local
`--asset-dir` until the EP-M4 release transport is connected. Independent
reviews covered all five requested specialities, with amendments recorded in
the design and checked by a final testing/consistency review. Planning
validation passed `make fmt`, `make markdownlint` (including spelling and three
spelling-helper tests), `make nixie`, and both working/staged
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

The 2026-11-09 candidate-bound re-assessment at `352b53f` raised six findings
and two evidence gaps against the same EP-M3 boundary. All eight are repaired
in the working tree: activation now runs the runtime-file contract before the
probe, `run` executes the identity it validated, the toolchain check enforces
two-way shared-object coverage and the probe routes libraries through the
verified toolchain, `pyroot` is validated as a contained setup addition, the
toolchain identifier is confined to one plain name, and deadline checkpoints
span local copying, hashing, extraction and validation. The evidence gaps are
closed by two spawn-process tests in
`tests/test_kani_snapshot_interruption.py`. Focused runs passed 139
install/manifest/toolchain/probe/run/inventory tests and 11
concurrency/interruption/activation tests. The full sequential gate stack then
passed on the same candidate: 1163 passed, 5 skipped, with lint, typecheck,
spelling, Markdown and Mermaid checks clean.

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

The current rebase target is `be04803d22a5e76677609b740b488fcda40f78b7`
(2026-11-08). Both existing planning commits were replayed. The CLI design's
append conflict was resolved by preserving main's complete implemented
`kani exec` section and the separate proposed snapshot link. Weave 0.5.1 and
its matching driver were inspected; no changed path selects that driver, so
Git's native `zdiff3` merge handled the conflict. Semantic `sem` diffs and Git
range/target diffs distinguish this documentation change from main's
implemented features. Main's manifest and lockfile are retained unchanged;
there was no lockfile conflict requiring regeneration.

The previous local CV-005 loader/readers were removed in favour of the pinned
shared `cv005-contracts` package. Run `make test-workflow-contracts`; preserve
`.github/cv005.toml`, coverage environment isolation, matching coverage Python
versions and mutation caller interpreter selection. Do not resurrect removed
`loading.py` or `reading.py` modules. New snapshot-specific contract tests
should follow existing pure-rule/refusal-fixture conventions and use a
supported strict parser boundary, testing duplicate keys and YAML `on` forms;
shared CV-005 coverage checks do not establish snapshot publication safety.

Main routes Makefile uv invocations through `scripts/uv_gate.py`, using the
shared cache and offline-first bounded fallback. Use `make prepare` and that
helper for focused Python commands; do not edit the vendored helper or
reintroduce the removed hash test. Spelling is the documented direct-uv
exception: `make spelling` runs pinned `typos-config-builder gate`, which may
regenerate `typos.toml`; old `spelling-config` and helper-test targets are
gone. Never hand-edit generated policy or weaken spelling. Review any
regeneration separately and keep unrelated dictionary changes outside this
planning patch.

`make check-fmt` checks tracked/unignored Markdown with `mdtablefix`; Pylint
remains pinned on managed PyPy 3.12 with syntax errors enabled. `make lint`
also runs interrogate 1.7.0 over package, tests and scripts: require docstrings
for every module, class and function, 100 percent coverage and no `MISSED`
rows. New tests and build helpers must satisfy this gate too. Runtime Python
remains 3.14; use the current named Ruff rule selections without suppressions.

`rust_prover_tools/cli.py` constructs the Cyclopts apps. Release installation
now defaults to pinned prebuilt Kani 0.67.0 frontend and backend assets through
`kani_binary.install_kani_binary`; `--compile` explicitly selects the older
Cargo source installation. Preserve both release behaviours, `VERSION` and
existing help/environment contracts. The snapshot commands remain additive and
must never enter the release source-build fallback. `check_kani_version` still
compares semantic versions and is not snapshot identity verification.

`kani exec` is an existing release-qualified API, implemented through
`kani_exec_cli.py`, `kani_release.py`, `kani_release_config.py` and
`kani_release_flags.py`. It selects an explicit installation, validates release
metadata/runtime paths, probes the effective compiler, retains configured
admission wrappers and isolates supported Cargo backend and coverage flags. It
does not install. Its initial 0.67.0/Linux qualification is not evidence for
the snapshot Rust 1.94 floor. Preserve it while adding snapshot-only
`kani run`. Reuse tested pure configuration/flag/path helpers where their
contracts fit; do not import fixed release compiler strings or LLVM ABI
assumptions into snapshot identity. Snapshot probes and proof execution must
retain configured `RUSTC_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER` and `RUSTDOC`
admission routes, rejecting malformed or unprovable routes rather than
disabling them. Preserve main's child-only profile backend overrides,
package/include rejection and exact coverage-flag filtering without mutating
the parent environment.

`commands.run_command` uses pinned Cuprum `0.2.0b2`, `ExecutionContext` and
`RunOutputOptions` with best-effort echo. `CommandSpec.env` remains an overlay;
replacement/removal, deadlines, descendant cleanup and bounded capture are
still explicit snapshot adapter work. Read the bundled Cuprum 0.2 migration and
user guides, and preserve `exit_status.guard_shutdown_flush`, safe
`print_lines` and closed-pipe status tests when extending handlers. Best-effort
echo must not turn a successful proof into failure or hide a verifier failure.

`binary_assets.PinnedAsset`, `downloads.download_pinned`, `download_url`,
`download_transport` and `download_stream` provide bounded HTTPS transfer,
redirect checks, digest validation and verified cache reuse. Extend these seams
with a tested snapshot-specific host/redirect policy where useful: current
HTTPS policy allows arbitrary hosts and is not D4's allowlist. Fixed release
pins are not manifest authentication. `kani_archive.extract_frontend` supports
only two regular launcher files and rollback of that pair; it is not backend
link-graph extraction or concurrency-safe generation activation. The global
Cargo-bin/version-keyed installer cannot serve as the snapshot install store.
Use shared `platforms`, `path_resolution` and safe observability helpers where
appropriate, without inheriting release-only limits or Verus assumptions.

Unit tests live in `rust_prover_tools/unittests/`; relevant files are
`test_kani.py`, `test_kani_binary.py`, the `test_kani_release*` suites,
`test_kani_exec_cli.py`, download tests and command adapter tests. Preserve
`tests/test_kani_exec_codegen_backend.py`, best-effort echo, closed-pipe CLI
exit-status and multi-file Verus regressions when extending shared adapters.
Public behaviour uses `tests/features/prover_tools_cli.feature` and
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
technical design is `docs/kani-snapshot-distribution-design.md`, revision 4,
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
and publish the next ordered activation record. Never relocate a setup home or
delete working generations. Recheck receipt, inventories and effective compiler
before execution. Keep immutable payload/toolchain files separate from narrowly
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
the snapshot adapter relies on them. `uv.lock` now resolves Cuprum `0.2.0b2`;
the required expanded adapter guarantees still need real-process evidence. A
dependency change or bypass requires deviation approval.

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
and field mutations. Run
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_pins` and the
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
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_integrity`. Use a
valid signed fixture through a faithfully modelled CLI response plus EP-M5's
real verifier boundary. Include wrong Rustup mirror/component bytes and
missing-toolchain cases; assert no compiler probe or auto-install can occur. A
negative control that skips signature/hash validation must reach a forbidden
execution spy and fail. Mocked signature success does not prove real signing.

### V3: Extraction stays contained

Invariant: all writes and links remain inside the private extraction root,
ordinary executable modes/contained links survive, and resource limits are
respected. Lemma: a validated member graph plus link-last no-follow
materialization prevents a link member redirecting later writes. Add
`test_kani_snapshot_archive.py`; combine named tar fixtures and Hypothesis
member graphs (up to 30 members and 16 link hops), with an independent
expected-path oracle and actual filesystem assertions. Run
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_archive --hypothesis-show-statistics`.
Witness regular files, contained hard/symbolic links and executables. Negative
controls include parent traversal, link-chain escapes, cycle, duplicate path,
special file, huge declared size and actual stream overrun. Apply effective
GNU/PAX header overrides, file/directory collisions and member-order
permutations; hard links must target contained regular files. Removing
containment checks must change an outside sentinel or trigger a forbidden-write
assertion. Generated cases complement, but do not prove, arbitrary filesystem
safety.

Evidence (2026-11-09): `test_kani_snapshot_archive.py` now generates contained
graphs of 2–10 unique paths, one to three components deep, with regular files,
symbolic and hard links, and asserts the result against an oracle that records
the expected directories, bytes, modes and link targets. Generated cases cover
member-order permutations, link chains of 1–16 hops, and the refusal cases for
parent traversal, escaping symbolic links, cycles, duplicate paths, special
members, size bombs and truncated bodies, each with an outside sentinel. The
ten generated tests and the existing named fixtures pass; removing containment
checks changes the sentinel or fails a forbidden-write assertion.

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
lock timeout. Run
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_install` and
`python scripts/uv_gate.py run -- pytest -q tests/test_kani_snapshot_concurrency.py`.
Both first-install failure and repair with an old working generation must be
reachable. A seeded premature pointer write or post-setup directory move must
fail; otherwise the model is insufficient. POSIX locking/rename durability and
private cache ownership are axioms tested at the integration boundary, not
formally proven.

Evidence (2026-11-09): `tests/test_kani_snapshot_activation.py` runs the
two-identity state machine for 8 generated examples of 30 operations each and
asserts after every step that the pointer names only the expected generation. A
deterministic sequence reaches both required branches: a refused first install
leaves no pointer, and a later repair after damage publishes a new generation
while the old directory survives. Seeded negative controls write a premature
pointer and move the active generation after setup; validation refuses both.
`tests/test_kani_snapshot_concurrency.py` uses spawn processes and a barrier
for overlapping installs, a `SIGKILL`ed lock owner and a bounded timeout; the
overlapping pair reports one generation and passes `check`.
`test_kani_snapshot_install.py` adds a same-reported-version regression proving
the two identities' homes and toolchains are disjoint.

Re-assessment evidence (2026-11-09): `tests/test_kani_snapshot_interruption.py`
kills an installer after it has started staging a repair, then proves the old
pointer is unchanged and the next install repairs from the intact source
release and reuses the preserved generation without restoring the damaged
cache. A second two-process case seeds a `ProverToolError` inside setup while a
peer has announced its attempt; the faulty actor reports failure, the peer
publishes the only active generation and `check` accepts it. Both use spawn
processes and events rather than sleeps for their barriers, so the assertions
observe real pipeline state rather than timing.

Re-assessment evidence (2026-11-10): a transient probe failure forces the
repair path while every artefact of the active generation stays intact; the
repair is killed after a partial staged write, and `check` with a healthy probe
validates the previous generation before any recovery runs, so an interrupted
repair cannot invalidate the evidence it was about to replace. A
barrier-controlled concurrency case damages the cached backend, stalls the
repair between payload verification and publication, and shows a reader with a
short lock timeout is refused by the lock rather than reporting the cache
damage; after the repair publishes, the same check succeeds. Unit cases bound a
write stalled in `fsync` under a real 0.5-second budget, and expire the budget
immediately before record publication, leaving no activation record and no
`.pending-*` document.

### V5: Execution selects the exact distribution

Invariant: absolute launcher/backend/toolchain paths correspond to the pinned
manifest regardless of earlier PATH entries; forwarded verifier arguments and
stdout/stderr/status survive. Add `test_kani_snapshot_run.py`,
`test_kani_snapshot_identity.py`, `tests/test_kani_snapshot_cli.py`, and
`tests/test_prover_tools_snapshots.py` variants. Use cmd-mox and real CLI fake
executables with spaces, shell metacharacters, conflicting environment,
identical upstream semantic versions, wrong compiler/backend, missing setup
state and distinct child statuses. Run
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot`. Assert no
`cargo install`, source checkout, solver build or implicit installation command
during install/check; forbid unwanted PATH Kani via an executable sentinel.
Include real environment removal, cwd, streaming and descendant timeout cleanup
tests. Normalize temporary directory prefixes only in Syrupy; retain SHAs,
digests, target, compiler and differing failure output. Switching to PATH or
masking a nonzero verifier status must fail named tests.

Re-assessment evidence (2026-11-09): `run_snapshot` now executes the generation
identity that `check_snapshot` validated, so a pointer rewritten between
validation and execution cannot redirect a proof.
`test_kani_snapshot_run.py::test_run_executes_the_generation_it_validated`
installs a second generation with a differently exiting launcher, publishes a
new activation record from inside the probe hook, and asserts the proof still
reports the validated generation's status (7). The probe contract also pins
`LD_LIBRARY_PATH` to the verified toolchain's `lib` directory in
`test_kani_snapshot_probe.py`, so an inherited Rustup toolchain cannot shadow
the libraries the toolchain check approved.

### V6: The recipe uses one upstream identity

Invariant: source, launchers and backend derive from one full SHA with
unchanged locks/toolchain pins and recorded recursive submodules. Add
`test_kani_snapshot_build.py` for command sequencing, immutable resolution,
malicious manual inputs, unchanged HEAD with changed recipe, missing/failed
upstream checks, runtime/licence completeness and resource-limit rejection. Run
`python scripts/uv_gate.py run -- pytest -q -k kani_snapshot_build`; compare
before/after source status and input hashes in the opt-in EP-M1/EP-M5
experiment. The negative control resolves a different SHA at a later phase or
drops a submodule record; the recipe must reject it. A mock cannot establish
upstream bundling support; real clean setup and smoke evidence discharge that
external boundary.

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
operations and named transition witnesses. Run the focused publication
contracts:

```bash
python scripts/uv_gate.py run -- pytest -q \
  -k 'kani_snapshot_publish or workflow_contract or python_release'
```

A seeded publish-before-upload or accepting cross-run receipt must fail. Assert
trigger namespace, scheduled disable default, dispatch validation, permissions,
SHA pin shape, no PR/fork writes, timeout/cache/concurrency contracts and
unchanged Python wheel namespace. Run actionlint and optional act per EP-M4;
real GitHub permissions/signing and immutable enforcement remain hosted-only
assumptions.

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
make test-workflow-contracts
```

`make markdownlint` includes the shared spelling gate and generated policy
regeneration; inspect resulting diffs. `make fmt` is required after Markdown
edits; inspect formatter changes before staging. Do not weaken gates or
hand-edit generated spelling configuration. Makefile/workflow checks apply when
those surfaces change; record absence of optional local tools honestly.
Planning-only gates are `make fmt`, `make markdownlint` and `make nixie`, plus
`git diff --check`; no runtime code or workflow is added in this invocation.

Focused acceptance examples after their test files exist:

```bash
python scripts/uv_gate.py run -- pytest -q rust_prover_tools/unittests/test_kani.py
python scripts/uv_gate.py run -- pytest -q -k kani_snapshot --hypothesis-show-statistics
python scripts/uv_gate.py run -- pytest -q tests/steps/test_kani_snapshot_steps.py
python scripts/uv_gate.py run -- pytest -q tests/test_prover_tools_snapshots.py
python scripts/uv_gate.py run -- pytest -q tests/test_kani_workflow_contract.py tests/test_python_release_contract.py
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

2026-11-08: revision 4 rebases onto `be04803`, preserves the implemented
release CLI and adds explicit reuse boundaries for binary downloads, release
execution and Cuprum 0.2. It replaces removed local CV-005 references with the
shared gate and adopts uv-gate, interrogate and current spelling conventions.
Admission wrappers remain active through compiler probes and proofs. Historic
September gate results above are not validation of this new candidate; current
rebase results are recorded in the planning PR. No implementation is approved.

Cuprum 0.2 exposes `EnvMode.REPLACE`, `UNSET` and run-level timeouts. Extend
the typed command adapter using those supported primitives, not custom
environment emulation. Its documented termination targets the direct child;
process-group ownership and descendant cleanup still require the EP-M1
experiment and real child-process tests before adoption.
