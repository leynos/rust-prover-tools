# Prover tools CLI design

## Context

The project now provides a Python package command named `prover-tools` to
replace four shell-script workflows used by Kani and Verus automation. The
command surface is intentionally small:

```plaintext
prover-tools kani install
prover-tools kani check-version
prover-tools kani exec --installation "$HOME/.kani/kani-0.67.0" \
  --cwd "$PWD" --extra-arg=list
prover-tools verus install
prover-tools verus run
```

`kani exec` requires a selected, already installed release. Its supported
platform, configuration, and trust constraints are documented in the
[users' guide](users-guide.md#explicit-legacy-kani-execution).

The implementation preserves the source scripts' externally observable
behaviour while moving command construction, validation, and test doubles into
typed Python modules.

## Architecture

`rust_prover_tools.cli` owns Cyclopts application construction, environment
variable binding, exit-code mapping, and output printing. Command functions are
thin adapters. They construct option dataclasses and delegate to feature
modules.

`rust_prover_tools.commands` is the only module that invokes external
programmes. It builds Cuprum `CommandSpec` values and returns a small
`CommandResult` containing standard output, standard error, and exit status.
Expected non-zero exits are translated to `CommandFailedError` by `run_checked`.

`rust_prover_tools.versions` contains pure parsing and pin-file helpers:
semantic-version validation, version extraction, checksum row lookup, and Verus
toolchain parsing.

`rust_prover_tools.kani` owns Kani installation and installed-version checks.
It keeps command strings shell-free by parsing overrides with `shlex.split`.

`rust_prover_tools.verus` is a public package facade. Its implementation is
split into:

- `rust_prover_tools.verus.models`: dataclasses, constants, and path helpers.
- `rust_prover_tools.verus.install`: release archive download, checksum
  verification, extraction, and directory normalization.
- `rust_prover_tools.verus.run`: binary resolution, toolchain management, and
  proof execution.

## Command invocation

External commands are always invoked through Cuprum. The command catalogue is
created per command because the executable name is user-configurable for some
flows, especially `--kani-command` and `--verus-bin`. The catalogue still
declares the project name and developer documentation location so Cuprum error
messages can point maintainers at the internal command conventions.

The implementation does not use shell strings. User-provided command text is
converted into an argument vector with `shlex.split`, and every command is
executed as an argument vector.

## Test strategy

Unit tests cover pure helpers and command orchestration. They use cmd-mox to
intercept external commands on `PATH`, including `cargo`, `curl`, `sha256sum`,
`shasum`, `unzip`, and `rustup`.

Behavioural tests use `pytest-bdd` to verify public CLI scenarios for matching
and mismatched Kani versions, successful Verus proof execution, and missing
Verus toolchain reporting.

Snapshot tests use Syrupy for top-level CLI help output because the command
list, options, and formatting are part of the user interface.

End-to-end tests run the public Python module entry point with deterministic
fake Verus binaries. They do not download real Verus releases in the default
test suite.

Hypothesis property tests cover invariants in the pure helper layer, such as
valid semantic-version acceptance and supported Verus toolchain output forms.

## Decisions

The CLI is a package console script rather than a standalone file. This keeps
imports, test coverage, type checking, and linting inside the normal Python
package gates. The command inventory includes the additive `kani exec` route;
it does not replace the existing install and version-check commands.

The public interface is noun/verb based because it maps directly to the source
scripts while leaving space for future prover tools.

The implementation preserves legacy environment variables where source scripts
already exposed them, while also adding `INPUT_` bindings for GitHub
Actions-style configuration.

The Verus compatibility proof default is named `EXAMPLE_VERUS_PROOF_PATH` in
code. Callers should provide `--proof-file` for project-specific proof entry
points.

The Verus run path carries the selected release target into installer fallback.
This keeps `prover-tools verus run --target <target>` aligned with
`prover-tools verus install --target <target>` when the default binary is
missing.

The Verus toolchain check treats a successful `verus --version` probe as
sufficient evidence that Verus is usable. `rustup` is only required when the
version probe fails with a parseable missing-toolchain message and automatic
toolchain installation is enabled.

The Verus installer keeps the original checksum validation and extraction
contract, but adds bounded `curl` timeouts to avoid indefinite network hangs.

## Operational notes

The Verus installer's final delete-and-move operation is not atomic. Concurrent
installations to the same target directory can race, so callers that share
install directories across processes must serialize access externally.

Default tests must remain deterministic. Real network downloads, real prover
installations, and long-running proof suites belong in opt-in integration jobs,
not the default `make test` gate.

## Explicit legacy release execution

The separate `kani exec` verb executes a selected complete release without
installing it, while leaving the proposed snapshot-only `kani run` programme in
[PR #131](https://github.com/leynos/rust-prover-tools/pull/131)
unchanged. The initial compatibility boundary is Linux x86_64 Kani 0.67.0 and
its exact release compiler metadata. Runtime lookup is child scoped; compiler
identity queries and subsequent Cargo compilation retain every configured
admission wrapper. Wrappers are optional when absent; the runtime does not
create a substitute on hosted runners. Environment and Cargo configuration
validation reject malformed selections and unproved backend or wrapper routes.
The caller must trust the selected SDK and proof workspace, and keep their
files and Cargo configuration unchanged throughout the invocation, including
nested Cargo processes. Validation does not snapshot or lock those inputs,
authenticate SDK bytes, or make execution an untrusted-project sandbox. Cargo
build scripts and procedural macros execute normally with the caller's
permissions. The command itself does not install or copy assets or modify
global environment or services.

The SDK-child flag boundary also removes the exact bare LLVM coverage
instrumentation option and the bare `coverage` and `coverage_nightly` cfgs, in
joined or paired form. Cargo-llvm-cov's documented `--no-rustc-wrapper` mode
transports instrumentation through ambient flags, allowing this boundary to
isolate the SDK child while preserving parent measurement and configured
admission wrappers. The parent mapping remains unchanged. Similar cfg names,
valued cfgs, `trybuild_no_target` and other codegen options remain intact;
malformed paired cfg options fail explicitly. Parameterized instrumentation
options and older aliases are not guessed or silently removed.

This isolation cannot prevent a compiler wrapper from appending flags after
normalization. The default cargo-llvm-cov wrapper mode therefore remains
unqualified for nested Kani. Selecting ambient mode also changes coverage
selection semantics, so consumers must validate their measured report scope and
ratchet rather than assuming numeric equivalence.
