# Kani snapshot distribution design

Status: Proposed; explicit plan approval is required before implementation.
Revision: 3, 2026-09-27. Scope: `leynos/rust-prover-tools` only.

## Purpose and boundaries

Provide centrally built, immutable Kani snapshots for
`x86_64-unknown-linux-gnu`. Consumers download a matching prebuilt launcher and
upstream-format backend bundle instead of compiling Kani or its solvers.
Compiling a proof fixture remains normal verification. Setup may download the
exact upstream Rust toolchain; the backend archive is not assumed to contain a
complete Rust installation.

This is a `rust-prover-tools` build of an upstream Kani snapshot, never an
upstream release. Its reported Cargo package version remains unchanged. A
passing compatibility fixture does not establish full-crate Axinite
compatibility. Other platforms, containers, source-change watchers, automatic
consumer upgrades, Axinite changes, native Python extensions, and committed
upstream checkouts or binaries are excluded.

The [execution plan](execplans/kani-monthly-snapshot-distribution.md) sequences
implementation and verification. The [roadmap](roadmap.md) separates delivered
capability from the independently authorized first publication.

## D1: Explicit selection and immutable identity

Existing `prover-tools kani install`, `prover-tools kani check-version`,
`tools/kani/VERSION`, options, environment bindings, output, and release
installation behaviour retain their contracts. Add these proposed commands;
they are not existing APIs:

```bash
prover-tools kani snapshot install --pin tools/kani/snapshot.toml
prover-tools kani snapshot check --pin tools/kani/snapshot.toml
prover-tools kani run --snapshot-pin tools/kani/snapshot.toml -- --harness bounded_push
```

Snapshot `install` and `check` default `--pin` to `tools/kani/snapshot.toml`,
resolved below `--repo-root`. `run` requires `--snapshot-pin`; it never
installs implicitly. All three accept `--repo-root` and `--cache-dir`; a
relative cache path is relative to the repository root. The default cache is
`.kani-snapshots` below that root. Execution uses `cwd=repo_root`, defaulting
to the invocation directory; a pin located elsewhere does not change the proof
root. `snapshot install` also accepts `--asset-dir` for a local directory
containing the exact release files and their attestation, allowing the same
consumer path before publication and for offline transfers. This changes
transport only, never verification. There is no `--skip-verify`, source
fallback, snapshot command override, or mutable selector. All verifier
arguments following `--` are forwarded as an argument vector. Installation and
identity errors return 1; CLI syntax errors retain Cyclopts behaviour; verifier
status is preserved, with signal exits mapped to the conventional
`128 + signal` when necessary.

Snapshot commands reject legacy selectors supplied through flags or applicable
`INPUT_VERSION`, `INPUT_VERSION_FILE`, `INPUT_KANI_VERSION_FILE`,
`INPUT_EXPECTED_VERSION`, `INPUT_KANI_COMMAND`, or `KANI` environment
variables. The mere presence of an unused `VERSION` file is not contradictory.
New options have no environment bindings in schema version 1, avoiding
inherited generic `INPUT_` values accidentally selecting a snapshot. Help tests
must establish that rule even with the parent Cyclopts environment
configuration.

The version 1 TOML pin requires exactly `schema_version`, `repository`, `tag`,
`upstream_commit`, `builder_commit`, `target`, and `manifest_sha256`.
Repository is exactly `leynos/rust-prover-tools`; both commits are lowercase
full 40-hex Git SHAs; the target is the single supported triple; the manifest
digest is 64 lowercase hexadecimal characters. Unknown keys, wrong types
(including booleans as integers), duplicates, unsupported schema versions,
credentialled URLs, and malformed identities fail closed. The example below is
schematic; angle-bracket fields must be replaced with reviewed, actual values
before use.

```toml
schema_version = 1
repository = "leynos/rust-prover-tools"
tag = "kani-snapshot-2026-09-<12-hex-upstream-prefix>-r1"
upstream_commit = "<full-upstream-commit>"
builder_commit = "<full-rust-prover-tools-commit>"
target = "x86_64-unknown-linux-gnu"
manifest_sha256 = "<sha256-of-exact-manifest-bytes>"
```

The tag grammar is `kani-snapshot-YYYY-MM-<12-hex-upstream-prefix>-rN`, with a
valid month and positive decimal revision without leading zeroes. Prefix and
full upstream SHA must agree. Every changed build input, including builder
revision or recipe, needs a new identity; a tag collision never permits
replacement. The release tag points to the full builder commit, not to Kani's
commit. All comparisons use complete identities, not the tag prefix or Kani's
reported semantic version. A cache key includes repository, full upstream and
builder SHAs, tag, target, and manifest digest using an unambiguous canonical
encoding hashed with SHA-256.

## D2: Manifest, checksums, and trust

`manifest.json` is UTF-8 JSON with schema version 1. Hash its exact bytes,
including the final newline; do not parse and reserialize before checking the
pin. Generation uses deterministic key order and rejects duplicate object keys,
non-finite numbers, unknown required structures, and malformed UTF-8. A trusted
pin is reviewed and checked into the consumer repository. Downloads and release
notes cannot update it. The initial pin must be produced from verified
publication evidence and reviewed independently of the asset server.

The manifest records the publication identity and the full upstream and builder
commits; recursive source submodule path/commit pairs; source and builder
lockfile digests; recipe/configuration digests; exact commands and adjustments;
runner image identity; target, CPU baseline, dynamic loader, required shared
libraries and tested glibc baseline; Rust toolchain identifier, components,
`rustc -vV` identity and Kani compiler identity; launcher package version;
backend and solver versions, revisions and executable digests; build
prerequisite names, sources, versions and digests; workflow path, event, ref,
run URL, run ID, attempt and timings; cache outcomes; and payload assets with
role, basename, media type, byte length and SHA-256. Unknown identities block
publication instead of being filled with guessed dates or version labels.

Payload roles are backend bundle, launcher archive, redistribution notices, and
a separately bounded `inventory.json` (64 MiB maximum). The original bundle
layout is preserved. This inventory records paths, types, modes, link targets,
sizes and file digests for the distributable trees, so cache validation can
detect later corruption. The manifest hashes the inventory as a payload; the
inventory does not hash its container or manifest. Setup-created files are
recorded separately in a local receipt and checked against independently
validated toolchain identities and allowed layout rules. A local receipt is not
a replacement trust anchor.

Digest dependencies are deliberately one-way. Produce payloads first, then the
manifest containing their digests. Produce `SHA256SUMS` with rows for payloads
and `manifest.json`; neither the manifest nor checksums hashes itself or the
other checksum container. Produce `manifest.sigstore.jsonl`, an attestation of
the manifest, afterwards. It is verified cryptographically, not included in the
manifest's digest graph. The publisher verifies the complete set including the
attestation and checksums before publication. Checksums are a convenience; a
checksum downloaded beside an archive does not authenticate the archive.

Require both the pinned manifest hash and a GitHub build attestation before
extracting or executing payloads. Use an explicitly documented, prebuilt GitHub
CLI prerequisite through the command adapter; do not implement Sigstore
cryptography. Verification policy fixes repository, signer workflow
`.github/workflows/kani-monthly.yml`, issuer
`https://token.actions.githubusercontent.com`, source ref `refs/heads/main`,
source and signer digest equal to the pinned builder SHA, and provenance
predicate `https://slsa.dev/provenance/v1`; reject self-hosted signing runners.
Use `gh attestation verify` with a local `--bundle`, explicit policy flags and
JSON output. Parse only successfully verified results. Correlate certificate
identities with the pin, and the manifest's run/attempt with the verified
statement; do not treat arbitrary predicate fields as independently observed
facts. Trust roots come from the supported verifier distribution or separately
provisioned trusted storage, never from the untrusted asset directory.[^1]

Trust rests on consumer review, protected builder code, GitHub's identity and
signing infrastructure, the verifier distribution, upstream dependency
provenance, and the owner-controlled local cache. An attacker with the same
local user privileges can alter code between checking and execution; defending
against that is outside this model. Attestation establishes origin, not benign
source or bit-for-bit reproducibility. No reproducibility claim is made until
independent builds with identical inputs have been compared.

## D3: Upstream-compatible build

Resolve an upstream candidate once to a validated full SHA and use that SHA for
every source read and checkout. For scheduled runs inspect at most the 20
newest default-branch commits, selecting the newest with the required upstream
Linux checks completed successfully. Record the exact check names, run URLs,
head SHAs and conclusions; empty or ambiguous check sets fail closed. Manual
input is a full SHA, with the same checks and compiler-floor policy. Never
interpret arbitrary input as a shell fragment, ref option, or repository URL.

At that SHA inspect `.cargo/config.toml`,
`.github/actions/build-bundle/action.yml`, `.github/workflows/release.yml`,
launcher Cargo manifests, setup implementation, Rust toolchain files, locks,
submodules, smoke scripts, and component licences. Follow upstream bundle
creation rather than archiving a development installation. Build the matching
`kani` and `cargo-kani` launchers from that checkout using locked dependency
resolution. Preserve upstream version metadata and toolchain pins. Build outside
`/tmp`, with the shared Cargo cache; no private Cargo home workaround for lock
contention. Record dirty-tree checks before and after building.

Planning inspected upstream commit `4e31125fdb4f01fad95150415cbafe46e0587d66`;
immutable source links and named successful upstream workflows are recorded in
the execution plan. Its bundle mechanism is `cargo bundle -- VERSION`; launcher
packaging is separate. Its `nightly-2026-09-23` toolchain resolved to Rust
1.100.0-nightly in upstream logs, but the repository-owned compiler-floor proof
remains unexecuted. Setup supports local bundles and local toolchains; its
version-derived home confirms the need for per-identity `KANI_HOME`. Setup
re-extracts with external `tar`: validate the complete archive before setup,
require the expected single `kani-VERSION/` root, and validate destinations,
links and collisions after `--strip-components=1`. Remove inherited
`TAR_OPTIONS`; allow setup to consume only the same verified bytes in a
private, bounded generation. The feasibility experiment must show that setup
preserves D4's containment and resource limits.

The first post-approval milestone is an explicit feasibility experiment. It
must establish exact bundle commands, launcher build commands, local-bundle
setup arguments, private `KANI_HOME` support, toolchain layout and symlink
behaviour, CPU/runtime compatibility, Rust compiler floor, and notices. Its
recorded commands become the tested recipe. Any source patch, lock update,
nightly substitution, unsupported setup mechanism, licence obstacle, or
compiler below 1.94 blocks progression for a documented deviation decision.
Prefer trusted prebuilt prerequisites where upstream supports them; inventory
solvers and include required notices, licence texts and source availability
metadata for every redistributed component. Do not assume Kani's licence covers
all bundled components.

## D4: Bounded installation and reliable execution

Keep `rust_prover_tools.kani` for the legacy release path. Add a feature package
`rust_prover_tools.kani_snapshot` with `models.py`, `pins.py`, `manifest.py`,
`download.py`, `archive.py`, `install.py`, `identity.py`, and `run.py`. Use
frozen slotted dataclasses for validated pins, manifests, asset records,
options, and identity reports; parsing helpers remain pure. Add tested builder
and release helpers under `rust_prover_tools.kani_snapshot.build` and
`publish.py`; keep YAML and any script entrypoints thin. The dependency
direction is CLI to feature orchestration to pure models/helpers and command
adapter. No feature imports CLI or Verus implementation. Reuse
`read_required_file` only where its whitespace handling is appropriate; never
use it for exact-byte manifest verification. The permissive legacy checksum
lookup and first-semantic-version parser cannot enforce snapshot integrity.

All external commands use `rust_prover_tools.commands` and Cuprum. Extend that
adapter for explicitly tested process deadlines, environment replacement and
variable removal without global environment mutation, descendant cleanup on
timeout/cancellation, and streaming output with bounded capture. Keep stdout
and stderr separate. Use status inspection for proof execution and explicit CLI
exit mapping rather than the generic error-to-1 path. Exercise tiny real
processes for these contracts before relying on cmd-mox. Cuprum support is a
feasibility condition; do not bypass it or add dependencies silently.
Standard-library HTTP/file/archive operations are permitted; no new runtime
Python dependency is proposed. The package's actual Python floor is 3.14. Older
generic guidance mentioning Python 3.13 and Plumbum yields to the package
metadata and feature-specific Cuprum contract.

For remote installs allow HTTPS only, `github.com` release URLs for the pinned
repository/tag, and explicit documented GitHub asset redirect hosts. Validate
every redirect before following it; never forward authorization across hosts.
Reject credentials, query-based overrides, path tricks, unexpected ports,
loopback, private addresses, and unapproved redirects. No public `--base-url`
exists. The HTTP adapter is injected at the Python boundary for deterministic
localhost tests; that test fixture does not broaden production host policy. Use
15-second connect and 300-second per-asset total deadlines, at most three
attempts for transient network failures with bounded backoff, five redirects,
and a 30-minute overall installation deadline, including setup descendants.
Integrity/schema errors are never retried as transient failures.

Initial hard limits are 4 MiB manifest, 16 MiB attestation, 1 MiB checksums, 2
GiB per compressed payload, 8 GiB total extracted data, 100,000 members, 4 KiB
path length, and 16 link hops. Match advertised and actual byte counts; limits
also apply during streaming and decompression. Reject duplicate archive paths,
absolute paths, parent traversal, NULs, drive prefixes, sparse entries,
devices, sockets, FIFOs, ownership changes and privileged mode bits. Keep
ordinary executable modes and legitimate contained symbolic/hard links. Build
and validate a complete member/link graph before materialization, reject cycles
or dangling/escaping targets, create regular files before links, and never
write through a previously created link. Apply rules to effective GNU/PAX
headers after overrides, reject file/directory collisions, and require
hard-link targets to resolve to contained regular files. Extraction uses a
private directory with no attacker-writable ancestors. Python's extraction
filter alone is not the complete containment policy. Reject unsupported hosts
before any download.

Rustup transport is a separate boundary. Preflight supported prebuilt GitHub
CLI, Rustup and required linker/runtime packages before staging. Pin their
trusted origins and versions. Override or remove `RUSTUP_DIST_SERVER`,
`RUSTUP_UPDATE_ROOT`, deprecated `RUSTUP_DIST_ROOT`, and inherited Cargo/Rustup
homes/configuration; permit only authenticated official Rust distribution
endpoints. Bind required component archive URLs, lengths and digests, channel
metadata digest, and extracted compiler/library inventory to the trusted
manifest or its hashed inventory. Verify downloaded and extracted compiler
bytes before running identity probes; version text alone is spoofable.
Establish this ordering against actual Rustup in EP-M1 or block for a design
deviation. Enable controlled prebuilt toolchain installation only during
explicit install; set `RUSTUP_AUTO_INSTALL=0` for check/run and test that
missing toolchains fail without network repair.[^4]

Under the cache key, use an advisory `fcntl.flock` lock on a stable lock file,
with a bounded wait and automatic kernel release on process death. Create an
unreferenced unique generation directory at its permanent location. Perform
extraction and setup there using generation-local `KANI_HOME` and isolated
Rustup state as required by the upstream experiment. Do not rename a setup home
containing absolute symlinks. Validate assets, toolchain, backend and receipt;
flush files and parent directories before atomically replacing a small
`active.json` pointer. Readers resolve and validate this pointer while holding
the key lock. An interrupted stage is never active. Existing validated
generations remain intact on failure. Immutable payload/toolchain trees are not
mutated or automatically garbage-collected; repair creates a new generation at
the same identity and switches only after validation. This avoids deleting
paths used by a running proof. Detect corrupt receipts, missing files, wrong
modes, changed symlinks and payload hashes on cache reuse, before running
probes. Inventory actual upstream writes during feasibility. Give narrowly
named bookkeeping/lock/scratch files separate generation-private mutable state;
never exempt arbitrary subtrees from integrity checking. Two concurrent proofs
must leave immutable trees unchanged and both normal user homes untouched.

Resolve launchers to absolute paths, force generation-private homes, select the
exact toolchain and backend, and remove conflicting inherited Kani/Rust
selection variables. Include `RUSTC`, `RUSTC_WRAPPER`,
`RUSTC_WORKSPACE_WRAPPER`, `RUSTUP_TOOLCHAIN`, and `RUSTFLAGS` in environment
contract tests. Preserve ordinary verifier output, split stdout/stderr and exit
status; do not invoke a shell. The snapshot wrapper must reject forwarded
setup/update and any upstream option that would replace its backend or
compiler. Record that finite selector list from upstream source and test it.
Normal harness/proof options remain transparent. Snapshot execution is not a
sandbox for untrusted proof crates or their Cargo configuration/build scripts.

An identity report states selected tag, full SHAs, manifest digest, target,
absolute launcher/backend/toolchain paths, upstream package version and actual
compiler identity. Compare executable digests and effective compiler
`rustc -vV`, not the host's default `rustc` or the first version in
`cargo kani --version`. Record and validate setup-generated paths. Reports omit
tokens, signed redirect URLs and inherited secret-bearing environment values.

## D5: Workflow and immutable publication

Propose `.github/workflows/kani-monthly.yml`, independent of Python releases.
Schedule `23 4 3 * *` is 04:23 UTC on the third day of each month. Scheduled
execution requires `vars.KANI_SNAPSHOTS_ENABLED == 'true'`; absent or other
values disable it. Manual `workflow_dispatch` inputs are `upstream_commit`
(required full SHA), `publication_id` (required tag grammar), and `publish`
(boolean, default false). Non-publishing mode builds and validates without
creating a release. It still creates a build attestation using OIDC; that side
effect must be explicit in dispatch documentation. No PR trigger is added to
this expensive workflow.

All privileged work requires canonical repository identity, event equal to
schedule or dispatch, and `github.ref == 'refs/heads/main'`. Resolve and record
builder `github.sha`; verify every trusted checkout against it. Manual runs
from other refs fail before building. Short-lived tokens and
`persist-credentials: false` keep credentials out of source checkouts.

Use distinct fresh jobs: resolution; unprivileged source build; trusted
manifest assembly and attestation; clean installation/proof validation; and
publication. Top-level permissions are empty. Resolution/build/validation
receive only required read permissions. The attestation job alone receives
`id-token: write` and `attestations: write`, plus required reads. Publication
alone receives `contents: write`. Upstream source and asset-supplied scripts
never execute in attestation or publication jobs. Those jobs run only checked
out builder code at the recorded trusted SHA and pinned Actions. Treat build
metadata as untrusted data: validate bounded schemas and correlate it with
resolution outputs and independently computed asset hashes before signing.

Attestation precedes clean validation to allow the public consumer verifier to
exercise signature policy on the exact manifest before publication. It is a
statement of build origin, not successful proof testing. A separate validation
receipt binds manifest digest, complete asset set, run ID and attempt, fixture
identities and observed harness results. Publication requires the successful
validation job and matching receipt from that same run/attempt. Retrieve
artefacts by exact same-run IDs, never by an arbitrary name in another run. Do
not unpack payload archives in the privileged job or import Python modules from
artefact directories.

Use full 40-hex Action/reusable-workflow pins; contract tests assert paths and
pin shape rather than today's values. Proposed runner is the existing hosted
Ubuntu x86-64 class, with an explicit image version selected during
feasibility; no larger paid runner is implied. Proposed job deadlines are 10
minutes for resolution, 180 for build, 15 for attestation, 45 for clean
validation and 15 for publication. Exceeding runner/disk/budget limits requires
review rather than automatic upgrades. Record wall time, cache hit/miss, disk
high-water mark, asset sizes and failures in run summaries. Cache keys bind
trust class, OS, target, upstream SHA, recursive submodules, toolchain, locks
and recipe; privileged jobs do not restore writable source-build caches.
Ordinary PR caches cannot feed signing or publication.

Serialize the entire snapshot workflow, starting before resolution and revision
allocation, with `cancel-in-progress: false` and a snapshot-specific
concurrency group. Recheck identity availability before create. Use the default
single pending slot and document that later runs can replace pending work; this
is not a durable monthly queue. Monitor cancelled/missed runs and recover
manually rather than promise exactly-once scheduling.[^5] Selection compares a
full build-input fingerprint, not upstream HEAD alone. Fingerprint source and
recursive submodules, builder/recipe, toolchain, dependency locks, prerequisite
digests, target and relevant build configuration. Exclude observed run/attempt,
timing, cache outcomes and asset sizes from input equality. These remain
manifest observations, not reasons to rebuild an unchanged input set. For a
month/prefix choose the next unused revision when inputs changed. If an
existing published identity exactly matches inputs and all assets and
provenance validate, return a recorded no-op; disagreement is a collision
error. A manual identity is never silently renamed. Source resolution and
release pagination are bounded; exhausting the bound is an actionable failure.

Create a draft pre-release with `make_latest: false` and a tag at the recorded
builder commit. Upload all payloads, manifest, checksums and attestation,
re-download and verify exact bytes and the complete expected asset set, then
publish. Reject an existing tag pointing elsewhere. Never clobber assets, move
tags, replace published bytes or delete history. An interrupted draft may be
resumed only with the same run/attempt's validated asset IDs and exact matching
existing bytes; upload missing assets only. Changed inputs, changed attempt,
unknown provenance, extra assets or conflicting bytes require a new identity
and leave the incomplete draft for manual diagnosis. Bounded retries within one
attempt can resume; a GitHub rerun increments the attempt and therefore
requires a new revision unless reconciliation finds the exact already-published
release from the previous attempt. A publication timeout is resolved by reading
state and validating it, not blindly repeating writes.

Repository-wide immutable releases are proposed for operational bootstrap, not
enabled by implementation or this planning task. First adjust
`.github/workflows/release.yml` to assemble all wheels, create a draft, upload
and verify all assets, then publish. Keep its `v*.*.*` namespace, wheel
contents and ordinary Python release behaviour, including generated release
notes. Require a non-empty expected wheel inventory; missing/extra assets,
interrupted drafts, published no-ops and collisions receive regression tests.
Add narrowly scoped lifecycle tests; this is necessary because the current
workflow creates a public release before uploading wheels. GitHub documents
draft-first publication for immutable assets and tags.[^2]

## D6: Evidence and operational handoff

Before publication a fresh runner with no Kani build checkout or previous
installation obtains only the builder's consumer wheel, produced assets,
attestation and repository-owned proof fixtures. Install through public
`snapshot install --asset-dir`, using a generated pin bound to the trusted
assembly job's manifest digest; then exercise `snapshot check` and `kani run`.
The fixture path is the working repository root for execution, not the builder
checkout. Run upstream bundle smoke tests in an unprivileged context, adapting
only fixture transport where the upstream script assumes a checkout.

Require named positive harnesses, a deliberately false assertion whose harness
ran and failed verification (not compilation), and a small compiler-floor
fixture requiring Rust 1.94 or newer. Use slice `array_windows::<2>()`,
documented as stable since 1.94, in a named successful Kani harness.[^6] Add a
pure-Rust version of that same function for the Rust 1.93 negative control:
failure must be the unavailable API, not Cargo's `rust-version`, Kani
annotations or dependency configuration. This control and the actual Kani
harness are executed only after approval. Include bounded collection
growth/drop behaviour and Resource Acquisition Is Initialization (RAII), where
ownership controls cleanup. Record compiler identity from the effective Kani
compiler, harness names/counts and statuses. No discovered harnesses, a
compilation error or unverified output is failure.

Repeat installation with a warm cache; intentionally corrupt a digest and
identity and show rejection before execution. Deterministic tiny distributions
with identical semantic versions but distinct identities prove isolation
without another full source build. Capture bounded descendant-process execution
evidence during clean setup (for example `strace -f -e trace=execve`, through
the command adapter). Reject `cargo install/build`, compiler code generation,
solver builds and equivalent source compilation anywhere in the process tree;
allow identity probes and prebuilt toolchain extraction. Adapter spies alone
miss children spawned by setup. Capture proof compilation as a separate phase;
setup toolchain downloads are separate evidence. Only proof execution may
compile fixtures.

Real attested EP-M5 validation requires approved builder code merged to trusted
`main`, with scheduling still disabled. Keep EP-M5 open until that hosted
non-publishing evidence exists; merging code alone does not satisfy it.

After implementation, write `docs/kani-snapshot-operations.md` with exact
settings, permission checks, default-disabled schedule, dispatch examples,
identity allocation, provenance verification, cold public-download commands,
draft recovery, rollback by older pin, and monitoring responsibilities.
Operational bootstrap separately enables prerequisites, dispatches a validation
run and first publication, downloads the public assets into another clean
environment, verifies release immutability/provenance, and enables the schedule
only after that succeeds. GitHub schedules can be delayed or dropped, run from
the default branch, and may be disabled after inactivity in public
repositories; monitor missed months and document manual recovery.[^3]

## Review record

The independent six-lens Logisphere panel reviewed revision 1 on 2026-09-27:
Pandalump/Dinolump covered Python architecture; Wafflecat/Pandalump Kani
packaging; Doggylump/Buzzy Bee release engineering and cost; Telefono
supply-chain contracts; and Telefono/Dinolump test design. The initial verdict
was revise/proceed with conditions. Revision 2 incorporates every requested
amendment: separate Rustup trust and no implicit install; real process
deadline, environment and child-execution evidence; hashed inventory payload;
explicit proof working directory and mutable-state boundary; serialized
allocation and stable input fingerprint; non-empty Python wheel
inventory/recovery; and substantive compiler-floor and proof transcript
controls.

Final testing/consistency review accepted these amendments, with revision 3
adding pinned upstream evidence, trusted-main sequencing and the shared build
budget.

Core bets remain conditional: supported upstream setup can isolate toolchains;
Cuprum can enforce bounded process semantics; and the selected runner can build
within measured limits. EP-M1 must demonstrate them. The strongest alternative
is private Rustup homes plus a shared read-only cache of verified toolchain
archives. Initially use private homes for simpler isolation, measure
duplication, and defer shared archive optimization.

The pre-mortem identified three incidents: an inherited Rust mirror spoofs the
compiler (prevent with bound component digests and sanitized environment); two
runs allocate one identity (serialize allocation and reject collisions); and
publication times out after success (read and verify actual state before
retrying). A fourth test concern, setup compiling a launcher invisibly, is
addressed by descendant-process evidence rather than adapter mocks alone.
Upstream source evidence and bounded feasibility questions are recorded in the
execution plan. No prototype, build, workflow dispatch, repository setting
change or publication is authorized by this document's proposed status.

[^1]: [GitHub CLI attestation verification](https://cli.github.com/manual/gh_attestation_verify).
[^2]: [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).
[^3]: [GitHub workflow schedule events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

[^4]: [Rustup environment variables](https://rust-lang.github.io/rustup/environment-variables.html).
[^5]: [GitHub workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
[^6]: [Rust slice ArrayWindows](https://doc.rust-lang.org/std/slice/struct.ArrayWindows.html).
