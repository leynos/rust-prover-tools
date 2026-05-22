# rust-prover-tools Users' Guide

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

Rust-enabled projects use `cargo nextest run` when `cargo-nextest` is available.
If `cargo-nextest` is not installed, the generated `test` target falls back to
`cargo test`. Rust documentation tests still run through `cargo test --doc`.

If cargo is missing from the local environment, generated Rust test targets fail
early with a clear error instead of falling through to an unusable `cargo`
invocation.

## Cleaning Local State

Run `make clean` to remove local build and cache outputs, including `.venv`,
`.uv-cache`, `.uv-tools`, Python cache directories, coverage outputs, and Rust
`target` output when the Rust extension is enabled.
