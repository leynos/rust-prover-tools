# Prover tools CLI design

## Context

The project now provides a Python package command named `prover-tools` to
replace four shell-script workflows used by Kani and Verus automation. The
command surface is intentionally small:

```plaintext
prover-tools kani install
prover-tools kani check-version
prover-tools verus install
prover-tools verus run
```

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
package gates.

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

The Verus installer keeps the original checksum validation and extraction
contract, but adds bounded `curl` timeouts to avoid indefinite network hangs.

## Operational notes

The Verus installer's final delete-and-move operation is not atomic. Concurrent
installations to the same target directory can race, so callers that share
install directories across processes must serialize access externally.

Default tests must remain deterministic. Real network downloads, real prover
installations, and long-running proof suites belong in opt-in integration jobs,
not the default `make test` gate.
