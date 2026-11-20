# rust-prover-tools Users' Guide

## Prover tools CLI

The `prover-tools` command provides one noun/verb interface for the Kani and
Verus workflows that were previously handled by repository shell scripts.

<!-- tested-example: prover-tools-commands -->
```bash
prover-tools kani install
prover-tools kani check-version
prover-tools verus install
prover-tools z3 install
prover-tools verus run --proof-file verus/edge_harvest_proofs.rs
```

`kani install`, `kani check-version`, `verus install`, `verus run`, and
`z3 install` accept `--repo-root PATH`, which defaults to the current working
directory. `kani exec` requires `--installation` and `--cwd`.

### Kani commands

`prover-tools kani install` reads `tools/kani/VERSION` below the repository
root and validates that it uses `MAJOR.MINOR.PATCH` format. By default it
installs the pinned prebuilt release on Linux x86_64:

1. Download the cargo-quickinstall frontend archive and the matching Kani
   native bundle, verifying both pinned SHA-256 digests.
2. Extract only the `cargo-kani` and `kani` executables into
   `$CARGO_HOME/bin` (or `~/.cargo/bin` when `CARGO_HOME` is unset).
3. Run `cargo kani setup --use-local-bundle <bundle>`.
4. Run `cargo kani --version`.

The prebuilt route only covers the release pinned by this package. Any other
version is rejected with a pointer to the compile fallback. Pass `--compile` to
build from source instead, which runs the older sequence:

<!-- tested-example: kani-compile-fallback -->
```bash
cargo install --locked kani-verifier --version <version>
cargo kani setup
cargo kani --version
```

Use `--version TEXT` to provide the version directly. Use `--no-setup` to skip
setup, and `--no-verify` to skip the final version probe. Use `--install-dir`
to place the extracted binaries elsewhere, and `--cache-dir` to choose where
verified archives are cached (default `.cache/kani` below the repository root).
The `KANI_INSTALL_DIR` and `KANI_CACHE_DIR` environment variables, with
`INPUT_`-prefixed forms, provide the same bindings. The `INPUT_KANI_COMPILE`
environment variable turns on the compile fallback just like `--compile`; pass
`--no-compile` to force the prebuilt route when that variable is set.

`prover-tools kani check-version` reads the expected version, runs the Kani
command with `--version`, parses the first semantic version from the output,
and fails if it differs from the expected version.

Useful options are:

- `--version-file PATH`: override the Kani version pin file.
- `--expected-version TEXT`: provide the expected version directly.
- `--kani-command TEXT`: override the command used to query Kani. The value is
  parsed with shell-like quoting rules but is not executed through a shell.

The legacy `KANI` environment variable is still supported for `check-version`.
For example:

<!-- tested-example: kani-check-version-environment -->
```bash
KANI='cargo kani --format=json' prover-tools kani check-version
```

### Explicit legacy Kani execution

`prover-tools kani exec` executes an already installed Kani 0.67.0 release on
Linux x86_64. It does not install tooling or download assets. The selected
installation must provide its matching `nightly-2025-11-21` toolchain through
`toolchain/` and the release-matched LLVM runtime. The toolchain path may be a
symlink into rustup's shared toolchain store. Cargo and rustc come from the
selected release's matching toolchain; the command does not require Cargo to be
physically stored inside the Kani archive. Compiler wrappers are optional: an
environment without them queries the selected compilers directly. When
configured, `RUSTC_WRAPPER` and `RUSTC_WORKSPACE_WRAPPER` retain their exact
values in compiler identity queries and execution, as does `RUSTDOC`. Empty or
malformed tool selections and unproved Cargo wrapper fallbacks fail before a
compiler starts. Cargo permits empty wrapper values to disable wrapping; this
command intentionally refuses that selection rather than remove a configured
admission boundary. Absent `RUSTDOC` is supported. Other releases and platforms
require separate qualification.

<!-- tested-example: kani-exec-invocation -->
```bash
prover-tools kani exec --installation "$HOME/.kani/kani-0.67.0" \
  --cwd "$PWD" --extra-arg=list
```

Pass each verifier argument literally through a repeated `--extra-arg` option,
using `--extra-arg=--harness=name` for arguments beginning with a dash. The
command preserves the proof working directory, output streams, and child exit
status (a signal becomes shell status `128 + signal`). It retains admission
wrappers and adds the matching runtime path only to the child environment.

Effective encoded compiler flags take precedence over plain flags, including an
explicitly empty encoded value. Development frontend threads, Cranelift, and the
`mold` linker selection are removed; compatible flags remain in order and
warnings are denied. Whitespace-containing encoded atoms cannot pass through
Kani 0.67's literal-space reader and are rejected. Independent profile backends
supplied through `RUSTFLAGS`, a caller-set
`CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` variable, unresolved Cargo configuration
includes, conflicting wrappers, protected routing environment overrides, and
unchecked `--config`, `--profile`, or `--manifest-path` arguments fail before
execution. Run from the proof package instead of selecting another manifest
through an argument.

A repository that selects a development backend in Cargo configuration or a
manifest profile is supported. `kani exec` accepts
`profile.<name>.codegen-backend` for the standard and custom profiles it
discovers, then sets `CARGO_PROFILE_<NAME>_CODEGEN_BACKEND=llvm` in the child
environment only. Cargo gives that variable precedence over both configuration
files and manifests, so the selected release keeps compiling with its own
backend and the caller's environment is unchanged. The boolean
`[unstable] codegen-backend` enable flag selects nothing on its own and is
accepted without an override. Profile names that contain characters other than
ASCII letters, digits, `-` and `_`, and package-scoped
`profile.<name>.package.<package>` overrides that the variable cannot shadow,
are refused before execution.

For an instrumented parent using cargo-llvm-cov's documented
`--no-rustc-wrapper` mode, the SDK child additionally removes the exact bare
`-C instrument-coverage` option and bare `coverage` and `coverage_nightly`
cfgs. Joined and paired spellings are supported. Other codegen options,
parameterized coverage options, custom cfgs and `trybuild_no_target` remain
caller policy. The parent environment is unchanged; compiler admission wrappers
remain configured. A missing paired `--cfg` value is rejected.

This boundary does not prevent a coverage wrapper from adding instrumentation
after flag normalization. Use the documented ambient-flag mode for this
integration; the default cargo-llvm-cov wrapper route is not qualified for a
nested Kani invocation. Removing these child flags does not disable parent
measurement. Coverage report scope and the local ratchet still need validation
when selecting that mode.

This legacy command is separate from the snapshot commands described in
[Kani snapshot commands](#kani-snapshot-commands).

### Kani log analysis

`prover-tools kani analyse-log` reads a captured Kani 0.67.0 verbose transcript
and decides what it proves against a harness manifest. The log supplies what
happened; the manifest supplies what was meant to happen. A printed success
line is never enough on its own: a harness can report success while proving
nothing, and a run can leave a declared harness out entirely.

Capture standard output and standard error separately, then concatenate them in
that order:

<!-- tested-example: kani-log-capture -->

```bash
prover-tools kani exec --installation "$HOME/.kani/kani-0.67.0" --cwd "$PWD" \
  --extra-arg=--verbose >kani.out 2>kani.err
cat kani.out kani.err >kani.log
```

Merging the streams with `2>&1` can interleave build chatter inside a report
block. The parser then refuses the log as structurally invalid rather than
guessing where the block ended.

#### Harness manifest

The manifest is a JSON object with exactly `schema_version` and `harnesses`.
Schema version `1` is the only accepted version. Each harness entry has:

- `name` — the harness name exactly as Kani reports it.
- `role` — `claim` or `control`.
- `covers` — the cover descriptions a claim declares; defaults to none.
- `expected_failure` — for a control, the check it must fail. A control has no
  default and must name one.
- `should_panic` — whether a failed claim is the panic Kani was asked to
  expect; defaults to `false`.

<!-- tested-example: kani-log-manifest -->

```json
{
  "schema_version": 1,
  "harnesses": [
    {
      "name": "harnesses::verified",
      "role": "claim",
      "covers": ["the boundary below saturation is reachable"]
    },
    {
      "name": "harnesses::expected_panic",
      "role": "claim",
      "should_panic": true
    },
    {
      "name": "harnesses::seeded_wrapping_mutant",
      "role": "control",
      "expected_failure": "assertion.1"
    }
  ]
}
```

Duplicate keys and duplicate harness names are refused, as are `NaN`,
`Infinity`, unknown keys, an unknown role, an empty harness list, a control
without `expected_failure`, a `should_panic` control, and a `should_panic`
claim that also declares covers. A control's expected failure may name a full
property identifier, the property within the harness, or a check kind. A
failure owned by a called routine must be named by its full identifier or its
kind, because its property name alone also names the control's own property.

#### Reading a log

<!-- tested-example: kani-log-analysis -->

```bash
prover-tools kani analyse-log --log kani.log --manifest harnesses.json
```

Useful options are:

- `--log PATH`: complete captured transcript.
- `--manifest PATH`: harness manifest declaring claims, controls and covers.
- `--harness NAME`: analyse only the named harnesses from the manifest. This
  option may be repeated; without it the whole manifest is analysed.
- `--require-full-profile`: exit with status `2` unless the run covered the
  whole selected manifest with unwinding checks enabled.
- `--format text|json`: output form, defaulting to text.

The analysis reports one outcome, taken from the strongest fact the log
supports. In decreasing precedence:

1. `counterexample` — a claim's own substantive check failed, a called routine
   failed, or Kani reported a failed verdict with no attributable failure.
2. `resource-exhausted` — a harness timed out, or an unwinding assertion ran
   out of bound.
3. `unsupported` — Kani could not model a construct the harness reached.
4. `undetermined` — a check was left undecided, or a cover was unreachable.
5. `skipped` — a declared claim never reported a block.
6. `verified` — every declared claim ran and nothing disproved it.

An unwinding failure is a bound, not a counterexample. A `should_panic` claim
is excused only when Kani also reported that the panic matched, and the run is
still bounded: none of these outcomes proves a property over every execution.

`tool-error` covers a log that cannot be decided from at all: truncated or
otherwise structurally invalid output, a release other than 0.67.0, `kani list`
inventory output, or a log that reports a harness the manifest does not
declare. It completes the analysis with exit status `2`; it is not an input
error.

Each declared cover is reported as `satisfied` or `unsatisfied`. A cover that
Kani reported unreachable, or did not report at all, is `unsatisfied`. Each
control is reported as one of:

- `rejected-as-intended` — every failed check, including one owned by a called
  routine, matches the expected property.
- `rejected-otherwise` — failed, but not only for the declared reason.
- `accepted` — passed, so it did not demonstrate the claim's guard.
- `not-run` — the log reported no block for it.

The run mode is `full-profile` only when the log reports exactly the selected
manifest's harnesses and unwinding checks are enabled in every block. Kani
disables those checks with `--no-unwinding-assertions` and otherwise relies on
CBMC's default, so a block whose echoed solver command lacks that flag counts
as enabled. A block that echoed no solver command is `focused`, because the log
cannot show what it was asked for; so is a block whose echoed command cannot be
split into arguments, since an unreadable command may still carry the flag.
Anything short of that is `focused`, because something could have been left out.

A run passes only when the outcome is `verified`, every declared cover is
`satisfied`, and every control is `rejected-as-intended`. The text form prints
`outcome:`, `reason:`, `run mode:`, one `cover` line per declared cover and one
`control` line per declared control.

With `--format json` the command writes one object with these keys, in order:

- `schema_version` — the analysis schema version, currently `1`.
- `outcome`, `reason`, `run_mode`, `passed`.
- `covers` — objects with `harness`, `cover` and `status`.
- `controls` — objects with `harness` and `status`.

Exit statuses are:

- `0` — the analysis passed and any full-profile requirement holds.
- `1` — a log or manifest could not be read, is not valid UTF-8, or the
  manifest is invalid.
- `2` — the analysis completed but did not pass, including `tool-error`.

Only Kani 0.67.0 transcripts are supported today. Output from `kani run`, its
retained 64 KiB tail, and Kani 0.68.0 snapshot logs are not valid analysis
input: `kani run` streams live output and deliberately keeps only the tail, so
it loses the banner and the start of the report. Capture a fresh run through
`kani exec` when you need an analysable log.

### Kani snapshot commands

The snapshot commands install, check and run one immutable, reviewed Kani
build. They are independent of the legacy `kani check-version` and `kani exec`
commands and never fall back to a host Kani installation.

A version 1 pin is a TOML document that requires exactly `schema_version`,
`repository`, `tag`, `upstream_commit`, `builder_commit`, `target` and
`manifest_sha256`. The only supported target is `x86_64-unknown-linux-gnu`.
Unknown keys, wrong types, duplicate keys, unsupported schema versions and
malformed identities fail closed. See
[D1: Explicit selection and immutable identity](kani-snapshot-distribution-design.md#d1-explicit-selection-and-immutable-identity)
for the tag grammar and field constraints.

`prover-tools kani snapshot install` materializes the identity named by
`tools/kani/snapshot.toml`:

<!-- tested-example: kani-snapshot-install -->
```sh
prover-tools kani snapshot install
```

Add `--asset-dir dist/kani-snapshot` to copy the same files from a local
directory instead of downloading the release.

Installation requires the prebuilt GitHub CLI (`gh`) on the caller's `PATH`;
the installer runs `gh attestation verify` and bundles no verifier of its own. A
`PATH` entry under a directory another user can write can replace `gh`, so run
the command with a `PATH` whose entries you trust.

Installation downloads the release the reviewed pin names: `manifest.json` is
authenticated against the pin's digest, and every payload and evidence file is
authenticated against what that manifest records. Add `--asset-dir` to install
from a local directory instead, which is useful for offline reproduction and
for reviewing a release before it is published. The directory must contain
`manifest.json`, `manifest.sigstore.jsonl`, `SHA256SUMS` and every asset the
manifest names. The installer checks, in order, that the manifest bytes match
the reviewed pin digest, that the pin binds to the manifest, that the checksums
agree, that `gh attestation verify` accepts the bundle under the repository
policy, and that every payload matches its recorded size and digest. It then
compares the backend archive's members with `inventory.json` and runs the
packaged launcher's `setup --use-local-bundle` with generation-private
`KANI_HOME`, `RUSTUP_HOME` and `CARGO_HOME`. Only after the installed
`rustc -vV` and `kani-compiler --version` match the manifest does the command
record the active generation. The same check that `snapshot check` performs
runs first: every runtime file the launcher needs must be present and
executable, `pyroot` must be a real directory whose links resolve inside that
directory and whose members exactly match the record written after setup, and
the recorded toolchain's shared libraries must live inside the private rustup
home with no unrecorded library beside them. The rustup home itself must also
resolve inside the generation. When an existing generation is reused, the
backend archive is rehashed rather than trusted by size, so an equal-size edit
is still refused. The compiler probes load the verified toolchain's `lib`
directory first, so an inherited `LD_LIBRARY_PATH` cannot shadow it.

`prover-tools kani snapshot check` revalidates the active generation without
running setup:

<!-- tested-example: kani-snapshot-check -->
```sh
prover-tools kani snapshot check
```

The command first validates the installed bytes — the receipt, cached payloads,
installed tree and toolchain files — without starting any process, then runs
the generation's `rustc -vV` and `kani-compiler --version` through the same
admission route a proof uses and compares both with the manifest. Both steps
hold the same shared lock, so the generation whose bytes were validated is the
one whose compiler is proved. The compiler step is a process: if you configure
`RUSTC_WRAPPER`, that wrapper runs and may write to its own caches or build
directory. The byte-validation step never invokes it.

`prover-tools kani run --snapshot-pin PATH -- ARGS...` executes a proof through
the active generation:

<!-- tested-example: kani-snapshot-run -->
```sh
prover-tools kani run --snapshot-pin tools/kani/snapshot.toml -- --harness proof
```

Every argument after `--` is forwarded unchanged, stdout and stderr are
mirrored live to the corresponding streams, and the launcher's exit status
becomes the command's exit status. A long proof therefore reports progress as
it runs rather than only on completion. Because a proof log can be arbitrarily
large, the command retains only the last 64 KiB of each stream for diagnostics
instead of buffering the whole run in memory. `run` revalidates the generation
first, so a changed payload or compiler stops the proof, and then executes
exactly the generation it validated: a concurrent change to the active
activation record cannot redirect the proof to a different build. It refuses
`setup`, `--use-local-bundle`, `--use-local-toolchain` and `-Z lean` because
they would replace the pinned backend.

Install and check default the pin to `tools/kani/snapshot.toml` and the cache to
`.kani-snapshots` below the repository root. Both accept `--pin`,
`--repo-root` and `--cache-dir`; `run` accepts `--snapshot-pin`, `--repo-root`
and `--cache-dir`. `run` also uses the repository root, which defaults to the
invocation directory, as the proof working directory.

Repeating an install while a validated generation is active reuses that
generation without reading `--asset-dir` again. A failed installation leaves
the previous generation and the active activation record unchanged; a new
generation is activated only after its receipt is durable, its compiler has
been observed, and every executable, recorded library, backend inventory member
and Python-root member it needs has been flushed. A cache whose payload
publication did not finish is refused by `check` and `run` until `install`
repairs it; a publication overtaken by a later successful install is not
unfinished, so a marker left behind by an abandoned attempt does not block the
cache. Concurrent installs for one identity serialize on an advisory lock with
a bounded wait (300 seconds) inside an overall 1800-second installation
deadline; the wait is also bounded by the time the installation has left, so a
slow earlier phase cannot push the call past its deadline. Staging, hashing,
extraction, tree validation, setup and the compiler probes all run within that
budget; the probes share the remaining time and refresh it before each command,
and expiry terminates the whole process group. A stalled setup therefore cannot
outlive the deadline or keep another installer waiting. A corrupt active
generation is repaired by installing a new generation, and the old directory is
preserved.

Checking and running never repair a generation. If `check` or `run` reports a
missing, changed or drifted snapshot, run `prover-tools kani snapshot install`
again with the verified assets.

Snapshot commands intentionally ignore the generic `INPUT_*` action bindings,
so a workflow input cannot silently redirect a reviewed pin or cache. Proof
execution is not a sandbox: the selected Kani build and the proof workspace
must still be trusted.

Cargo configuration discovery requires a non-empty `CARGO_HOME` or `HOME`. The
command uses the invoking environment and fails rather than guessing a home
directory when both are absent. Treat the selected SDK and proof workspace as
trusted; keep their files and Cargo configuration unchanged until this command
and its nested Cargo children finish. Cargo still runs build scripts and
procedural macros with the caller's permissions, so this compatibility route is
not a sandbox for untrusted projects.

Before execution, `snapshot run` validates the proof's Cargo configuration
discovered from the proof working directory and its ancestors, plus the
generation's private Cargo home. A malformed tool selection, an unresolved
include, an ambiguous configuration pair, a conflicting wrapper, a protected
routing override, or an unreadable configuration fails before execution.
Correct or remove the offending `.cargo/config.toml` or manifest profile and
run from the proof package. The
[Explicit legacy Kani execution](#explicit-legacy-kani-execution) section gives
the detailed rules.

### Snapshot maintenance workflow

`.github/workflows/kani-monthly.yml` builds, attests, validates and publishes
one immutable Kani snapshot. Scheduled runs are disabled unless the repository
variable `KANI_SNAPSHOTS_ENABLED` is set to `true`; any other value, including
an unset variable, makes the run report that it is disabled and finish without
building anything.

A manual dispatch takes three inputs:

| Input             | Meaning                                                                                                                      |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `upstream_commit` | Full 40-hex upstream commit to build. It must be the commit the pinned recipe names; another commit is refused at plan time. |
| `publication_id`  | The immutable tag to publish, in the form `kani-snapshot-YYYY-MM-<12 hex>-rN`.                                               |
| `publish`         | Whether to create the release after validation. Defaults to `false`.                                                         |

A dispatch always creates a build attestation with OpenID Connect, even when
`publish` is `false` and no release is created. A non-publishing dispatch
therefore still exercises signing and validation without making anything
public. Published snapshot assets are immutable: correcting a published
snapshot means choosing a new publication identity, never replacing or moving
existing bytes. An interrupted draft is resumed only by the same run and
attempt. Identity allocation reads the published tags alone, because GitHub
lists drafts only to a token that may push, so a draft left by an earlier
attempt is not counted when the next run numbers its identity. Publication
re-reads the tag with the write token and refuses a draft that records another
run or attempt, naming that disagreement instead of adopting it; remove the
stale draft, or choose a new publication identity, before retrying. A record
left by a failed upload that holds no bytes is removed and the file is uploaded
again, but only after any asset already in the draft whose digest GitHub did
not report has been downloaded and checked, so a draft that turns out to hold
conflicting bytes is never edited. Publication re-reads the draft once that
download finishes, uploads again anything that disappeared while it ran, and
refuses a draft that was meanwhile retargeted or rewritten to record another
attempt, so a release is not made public from a reading that went out of date
while it was being checked. The same confirming read follows every download
that authorizes a step, including the verification of an already published
release, and a verdict reached across a change is discarded rather than reused.

This guarantee covers changes the confirming read observes. A change made after
that read but before the update that makes the draft public can still become
public, because GitHub's release update has no conditional form that would
reject it. The read taken after the write reports such a divergence, but only
after it has happened. Changed inputs, conflicting bytes, or a release that
moved under the check require a new identity.

To dispatch a non-publishing run from a checkout:

<!-- tested-example: kani-snapshot-smoke -->
```sh
make kani-snapshot-smoke \
  KANI_SNAPSHOT_UPSTREAM_COMMIT=<full upstream commit> \
  KANI_SNAPSHOT_PUBLICATION_ID=kani-snapshot-2026-12-<12 hex>-r1
```

The target is opt-in and never part of `all`, because it builds on a hosted
runner and creates an attestation.

Ordinary Python releases are unaffected by the snapshot schedule. Pushing a
`v*.*.*` tag creates a draft release, uploads the wheel, verifies that the
release serves exactly the expected bytes, and publishes the release only
afterwards. A rerun resumes an existing draft. A rerun against a published
release is a no-op when the served assets match the expected wheel set and
bytes. A published release whose assets differ is refused as a collision
because its assets can no longer be changed.

### Verus commands

`prover-tools verus install` reads `tools/verus/VERSION` and
`tools/verus/SHA256SUMS`, downloads the matching Verus release archive,
verifies its SHA-256 checksum, extracts it, and normalizes the installation
directory to `<install-dir>/verus`.

Useful options are:

- `--version-file PATH`: override the Verus version pin file.
- `--checksum-file PATH`: override the Verus checksum file.
- `--target TEXT`: select the release target. The default is `x86-linux`.
- `--install-dir PATH`: select the installation directory. The default is
  `.verus/<version>` below the repository root.
- `--base-url URL`: override the Verus release download base URL.

The installer downloads the archive through the shared bounded HTTPS
downloader. It accepts only `https` URLs, applies a redirect cap, a 15-second
connect timeout, a 300-second total transfer deadline, and the 2 GiB archive
ceiling described under [download safeguards](#download-safeguards). The
archive is stored under a fixed local name, so a version or target pin cannot
select a path outside the temporary download directory, and the version and
target are restricted to a single safe path component.

`prover-tools verus run` resolves a Verus binary, checks the toolchain reported
by `verus --version`, optionally installs that Rust toolchain with `rustup`,
and then runs Verus against each requested proof file. Verus output is streamed
to standard output.

Streaming continues when the reader goes away. Piping into `head`, quitting a
pager, or an interrupted CI log collector closes the receiving end; the run
then stops displaying output, still captures everything Verus wrote, and the
command result carries Verus's own exit status rather than a broken-pipe
failure.

The process exit status survives that reader going away too. Python normally
replaces the requested status with 120 when it cannot flush its own standard
output at shutdown. That 120 is still a failure, but it is the same failure for
every run, so anyone reading the status through a pipe cannot tell a passing
proof from a failing one. `prover-tools` guards that flush, so the
`prover-tools` process itself still exits with Verus's status: `0` when the
proof passed, and the prover's own non-zero status when it failed. The guard
covers standard error as well, so redirecting both streams into one closed pipe
(`2>&1 | head -n 5`) reports the prover's status too.

A shell pipeline normally reports the status of its last command, not the
prover's, so `prover-tools verus run ... | head -n 5` succeeds even when the
proof fails. With `set -o pipefail`, the pipeline reports the rightmost failing
stage instead, which is the prover's status only when no later stage fails. To
read the prover's status from a pipeline that closes early, inspect the first
stage explicitly:

<!-- tested-example: verus-pipefail-status -->
```bash
# Read PIPESTATUS in each branch so `set -e` cannot skip the capture.
if prover-tools verus run --proof-file proofs/example.rs \
  | tee verus.log | head -n 5; then
  prover_status=${PIPESTATUS[0]}
else
  prover_status=${PIPESTATUS[0]}
fi
```

`PIPESTATUS[0]` is the prover's status because the prover is still the first
stage, even though the pipeline ends at `head`. `tee`'s log may stop when
`head` does; the run itself continues and keeps its own capture.

`PIPESTATUS` must be read before running any other command, because it is
overwritten by each one. Reading it inside both branches keeps the capture
reachable under `set -e`: commands in an `if` condition are exempt from
`errexit`. It is a Bash feature; `zsh` spells the same array `pipestatus`, and
its arrays start at index 1, so use the same `prover_status` variable with
`${pipestatus[1]}` there. Avoid the name `status` in `zsh`: it is a read-only
special parameter that aliases `$?`.

Official Verus releases may indent the `Toolchain:` line and append an
`RUSTUP_TOOLCHAIN` override message. `prover-tools verus run` accepts that
output and continues to install or use the reported Rust toolchain.

Useful options are:

- `--proof-file PATH`: proof entry point to verify. This option may be
  repeated, and every named file is verified. When omitted, the command uses
  the compatibility example path `verus/edge_harvest_proofs.rs`.
- `--verus-bin TEXT`: executable, directory, or command name used to locate
  Verus.
- `--install-dir PATH`: installation directory used for the default binary.
- `--target TEXT`: release target used if the command needs to install a
  missing default Verus binary. The default is `x86-linux`.
- `--no-ensure-toolchain`: skip automatic `rustup` toolchain installation.
- `--no-install-missing`: do not run the installer when the default binary is
  missing.
- `--extra-arg TEXT`: append an additional argument to each Verus invocation.
  This option may be repeated.

#### Verifying several proof files

Verus accepts only one input filename per invocation, so `prover-tools` runs
the verifier once per `--proof-file` value instead of passing several names at
once. Files are verified in the order given:

<!-- tested-example: verus-multiple-proof-files -->
```bash
prover-tools verus run --repo-root . \
  --proof-file verus/edge_harvest_proofs.rs \
  --proof-file verus/session_core_distance.rs
```

The command stops at the first failing file. The error message names that file
on its `Proof file:` line and lists any files already proved on a separate
`Proved before the failure:` line, so a partial run cannot be mistaken for a
total one. The process exits with the failing file's Verus exit status. Every
named file must exist before the first verifier invocation starts; a missing
path fails the run without proving anything.

The environment binding remains a single-file form: `VERUS_PROOF_FILE` and
`INPUT_PROOF_FILE` each supply one proof file. Repeating `--proof-file` on the
command line overrides the environment value.

Callers upgrading from the single-file release should read the
[0.2.0 migration guide](v0-2-0-migration-guide.md), which also covers the
`VerusRunOptions` API change.

The run command recognizes Verus binaries in these directory layouts:

- `verus`
- `verus/verus`
- `bin/verus`

If an explicit `--verus-bin` value is invalid, the command reports the invalid
override and falls back to the default installed binary. If the default binary
is missing and `--install-missing` is enabled, the command runs the internal
installer before retrying resolution.

The legacy Verus environment variables are still supported:

- `VERUS_TARGET`
- `VERUS_INSTALL_DIR`
- `VERUS_BIN`
- `VERUS_PROOF_FILE`

### Z3 solver commands

Verus uses a Z3 solver that it does not ship in its own archive.
`prover-tools z3 install` downloads the pinned official Z3 release for Linux
x86_64, verifies its SHA-256 digest, extracts only `bin/z3`, and records the
installed version and the extracted binary's SHA-256 in a marker beside the
binary. Every run rehashes `z3` before reusing it, so a modified or replaced
solver is reinstalled rather than trusted. A missing or invalid marker, and a
marker written by an earlier release, also cause one reinstall. A reinstall
uses the digest-verified archive already in the cache when it is present.

<!-- tested-example: z3-install -->
```bash
prover-tools z3 install
export VERUS_Z3_PATH=".cache/z3/4.16.0/z3"
```

Useful options are:

- `--install-dir PATH`: select the directory for the `z3` binary. The default
  is `.cache/z3/<version>` below the repository root.
- `--cache-dir PATH`: select the verified archive cache. The default is
  `.cache/z3` below the repository root.

The `Z3_INSTALL_DIR` and `Z3_CACHE_DIR` environment variables, with
`INPUT_`-prefixed forms, provide the same install and cache bindings.

### Download safeguards

Kani, Z3, and Verus downloads are bounded so a bad or hostile response cannot
exhaust disk space or hang a CI job:

- only `https` URLs are accepted, redirects are re-validated, and the redirect
  count is capped;
- each pinned asset declares a byte ceiling, and a `Content-Length` above that
  ceiling is rejected before the body is read;
- streamed bytes are counted, so a response without a length header is still
  cut off at the ceiling;
- one monotonic deadline covers the request that opens each hop, the header
  parse and every body read. A hop connects explicitly, then re-arms its socket
  with the time connecting left, so the send is not bounded by a budget the
  connection setup has already spent;
- a watchdog shuts the hop's socket down at the deadline, so a blocked send,
  a stalled header parse and a stalled body read all fail at the deadline
  rather than waiting out a socket timeout. A read or end of stream at or after
  the deadline is reported as the deadline, not as a retryable transport fault;
- bytes are written to a pending file that is removed on any failure, and the
  final path appears only after the pinned digest matches.
- downloads connect directly over HTTPS; proxy environment variables such as
  `HTTPS_PROXY` are not read;
- opt-in structured events are available through Python logging at debug level
  for download start, completion, cache hits, verification, and bounded failure
  categories, and for each installer's start, reuse, extraction, setup,
  verification, marker, and completion boundaries. A refused Z3 reuse also
  reports `z3.install.cache_miss` with one bounded reason (`binary_missing`,
  `not_executable`, `marker_missing`, `marker_unusable`, or `digest_mismatch`),
  so the usual reinstall causes are visible. A first install and a reinstall
  whose binary was deleted both report `binary_missing`. The library installs
  no handlers, so output is unchanged unless the application lowers the
  `rust_prover_tools` logger level.

The deadline is not a hard wall-clock bound on everything a download does. A
socket timeout bounds one blocking call rather than a sequence of them, so
these stay outside it:

- DNS resolution, which has no socket timeout at all;
- the per-address connection attempts the standard library makes inside one
  connect call, and the initial TLS handshake, which reuses the same budget, so
  a multi-address host can overrun the deadline by one hop timeout per address;
- local file I/O while the body is written, which the watchdog does not
  supervise;
- digest verification, which follows the transfer and is bounded only by the
  byte ceiling; publication, which has no time bound; and a retry, which is
  bounded by its attempt count and backoff.

### GitHub Actions inputs

Every CLI option can also be provided through an `INPUT_` environment variable
where the command declares a binding. This keeps the command usable from GitHub
Actions-style wrappers. For example:

<!-- tested-example: github-actions-inputs -->
```bash
INPUT_REPO_ROOT="$PWD" INPUT_PROOF_FILE=proof.rs prover-tools verus run
```

Legacy environment variables continue to work for compatibility with existing
automation. Explicit command-line options take precedence over environment
configuration.

## Quality Gates

Generated projects use `make all` as the standard local quality gate. It runs
these targets in order:

- `build`: create the local virtual environment and install development
  dependencies with `uv sync --group dev`.
- `check-fmt`: check Ruff formatting for Python sources and, when Rust is
  enabled, `cargo fmt` for the Rust extension.
- `lint`: run `lint-python` and, when Rust is enabled, `lint-rust`.
- `typecheck`: run `ty check`.
- `test`: run pytest and, when Rust is enabled, Rust tests.

The `lint-python` target runs Ruff followed by a pinned Pylint
(`PYLINT_VERSION`) on uv-managed PyPy 3.12 (`PYLINT_PYTHON`), installed through
`uv tool run`. `syntax-error` stays enabled, so a module that PyPy cannot parse
fails the lint rather than being skipped.

When the Rust extension is enabled, `lint-rust` runs:

- `cargo doc` with warnings denied;
- `cargo clippy` with the generated Clippy configuration; and
- Whitaker with `whitaker --all`.

The generated Makefile installs Whitaker on demand before local Rust linting
when it is not already available.

## Rust Test Behaviour

Rust-enabled projects use `cargo nextest run` when `cargo-nextest` is
available. If `cargo-nextest` is not installed, the generated `test` target
falls back to `cargo test`. Rust documentation tests still run through
`cargo test --doc`.

If cargo is missing from the local environment, generated Rust test targets
fail early with a clear error instead of falling through to an unusable `cargo`
invocation.

## Cleaning Local State

Run `make clean` to remove local build and cache outputs, including `.venv`,
`.uv-cache`, `.uv-tools`, Python cache directories, coverage outputs, and Rust
`target` output when the Rust extension is enabled.
