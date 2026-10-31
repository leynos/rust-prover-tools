# rust-prover-tools Users' Guide

## Prover tools CLI

The `prover-tools` command provides one noun/verb interface for the Kani and
Verus workflows that were previously handled by repository shell scripts.

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

This legacy command is separate from the snapshot programme proposed in
[PR #131](https://github.com/leynos/rust-prover-tools/pull/131).

Cargo configuration discovery requires a non-empty `CARGO_HOME` or `HOME`. The
command uses the invoking environment and fails rather than guessing a home
directory when both are absent. Treat the selected SDK and proof workspace as
trusted; keep their files and Cargo configuration unchanged until this command
and its nested Cargo children finish. Cargo still runs build scripts and
procedural macros with the caller's permissions, so this compatibility route is
not a sandbox for untrusted projects.

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
installed version beside the binary. A repeated run reuses a marked
installation without another download.

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
- one monotonic deadline covers connection, redirects, and body transfer;
- bytes are written to a pending file that is removed on any failure, and the
  final path appears only after the pinned digest matches.
- downloads connect directly over HTTPS; proxy environment variables such as
  `HTTPS_PROXY` are not read;
- opt-in structured events are available through Python logging at debug level
  for download start, completion, cache hits, verification, and bounded failure
  categories, and for each installer's start, reuse, extraction, setup,
  verification, marker, and completion boundaries. The library installs no
  handlers, so output is unchanged unless the application lowers the
  `rust_prover_tools` logger level.

### GitHub Actions inputs

Every CLI option can also be provided through an `INPUT_` environment variable
where the command declares a binding. This keeps the command usable from GitHub
Actions-style wrappers. For example:

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
