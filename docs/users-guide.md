# rust-prover-tools Users' Guide

## Prover tools CLI

The `prover-tools` command provides one noun/verb interface for the Kani and
Verus workflows that were previously handled by repository shell scripts.

```bash
prover-tools kani install
prover-tools kani check-version
prover-tools verus install
prover-tools verus run --proof-file verus/edge_harvest_proofs.rs
```

`kani install`, `kani check-version`, `verus install`, and `verus run` accept
`--repo-root PATH`, which defaults to the current working directory.
`kani exec` requires `--installation` and `--cwd`.

### Kani commands

`prover-tools kani install` reads `tools/kani/VERSION` below the repository
root, validates that it uses `MAJOR.MINOR.PATCH` format, and runs:

```bash
cargo install --locked kani-verifier --version <version>
cargo kani setup
cargo kani --version
```

Use `--version TEXT` to provide the version directly. Use `--no-setup` to skip
`cargo kani setup`, and `--no-verify` to skip the final version probe.

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

The installer keeps the shell-script behaviour of using `curl -sSfL`, but also
sets `--connect-timeout 15` and `--max-time 300` so stalled downloads fail
within a bounded interval.

`prover-tools verus run` resolves a Verus binary, checks the toolchain reported
by `verus --version`, optionally installs that Rust toolchain with `rustup`,
and then runs Verus against the proof file. Verus output is streamed to
standard output.

Streaming continues when the reader goes away. Piping into `head`, quitting a
pager, or an interrupted CI log collector closes the receiving end; the run
then stops displaying output, still captures everything Verus wrote, and the
command result carries Verus's own exit status rather than a broken-pipe
failure. That guarantee stops at the `prover-tools` process boundary. Over a
closed pipe the process itself can still exit 120, because Python reports that
its own standard output was closed while shutting down. An exit status read
through a pipe therefore cannot distinguish a passing proof from a failing one;
rely on the command result, or on an unpiped run, where the status is the
prover's own.

Official Verus releases may indent the `Toolchain:` line and append an
`RUSTUP_TOOLCHAIN` override message. `prover-tools verus run` accepts that
output and continues to install or use the reported Rust toolchain.

Useful options are:

- `--proof-file PATH`: proof entry point to verify. When omitted, the command
  uses the compatibility example path `verus/edge_harvest_proofs.rs`.
- `--verus-bin TEXT`: executable, directory, or command name used to locate
  Verus.
- `--install-dir PATH`: installation directory used for the default binary.
- `--target TEXT`: release target used if the command needs to install a
  missing default Verus binary. The default is `x86-linux`.
- `--no-ensure-toolchain`: skip automatic `rustup` toolchain installation.
- `--no-install-missing`: do not run the installer when the default binary is
  missing.
- `--extra-arg TEXT`: append an additional argument to the Verus invocation.
  This option may be repeated.

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
