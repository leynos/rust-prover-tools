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

All commands accept `--repo-root PATH`. When omitted, the current working
directory is used.

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

The run command recognises Verus binaries in these directory layouts:

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

The `lint-python` target runs Ruff followed by Pylint via a PyPy-backed runner.
The Pylint runner is installed through `uv tool run` from the pinned
`pylint-pypy-shim` repository.

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
