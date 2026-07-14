# Developers' guide

## Spelling policy

Run `make spelling` to enforce en-GB-oxendict spelling. Typos scans tracked
Markdown, while the exact-phrase checker scans tracked UTF-8 text so prohibited
forms are also caught in source comments and tests. The tracked `typos.toml`
starts from the shared estate dictionary and applies the narrow repository
policy in `typos.local.toml`. Edit the local policy, then run
`make spelling-config` rather than changing generated entries by hand. The
focused shared config builder refreshes its untracked dictionary cache only
when the authoritative copy is newer.

## Prover CLI internals

The `prover-tools` command is implemented as package code under
`rust_prover_tools/`. Keep command functions in `rust_prover_tools.cli` thin:
they should bind Cyclopts parameters, construct option dataclasses, call a
feature module, and translate expected `ProverToolError` exceptions to process
exit codes.

Feature modules should own workflow behaviour:

- `rust_prover_tools.kani` for Kani installation and version checks.
- `rust_prover_tools.verus.install` for Verus release installation.
- `rust_prover_tools.verus.run` for Verus binary resolution, toolchain checks,
  and proof execution.
- `rust_prover_tools.versions` for pure parsing and pin-file helpers.

Public helpers exported from `rust_prover_tools.verus` should keep structured
NumPy-style docstrings. That package is the stable import facade for tests and
future callers.

## External commands

Do not call `subprocess`, shell strings, or Plumbum from new prover workflows.
Use `rust_prover_tools.commands.CommandSpec` with `run_command` or
`run_checked`.

Build command arguments as tuples of strings. Parse user-provided command
strings with `split_command`; it uses `shlex.split` and rejects empty commands.

Use `run_checked` when non-zero exits should fail the workflow immediately with
a user-facing `CommandFailedError`. Use `run_command` when the workflow needs
to inspect the exit code, such as `rustup which` or `verus --version`.

Set `CommandSpec.echo=True` only when the child process output is part of the
public CLI stream, such as a Verus proof run.

## cmd-mox tests

Use cmd-mox for unit tests that exercise external command orchestration. The
test should mock or stub the executable name that production code passes to
Cuprum.

Prefer exact `mock(...).with_args(...).returns(...)` expectations when command
order and arguments are part of the contract. Use `stub(...).runs(handler)` for
commands that need to handle multiple argument shapes in one test.

Assert against `cmd_mox.journal` when the command sequence is an observable
contract. Include assertion messages that explain the intended command
behaviour.

Use `#!/bin/sh` for fake executable scripts in tests unless the test needs a
Bash-only feature. Set explicit `0o755` file modes for deterministic
executability.

## Documentation updates

When a CLI option, environment variable, command output, or exit behaviour
changes, update `docs/users-guide.md`.

When an internal command convention, module boundary, or testing practice
changes, update this guide.

When an architectural decision changes how the CLI is structured, update
`docs/prover-tools-cli-design.md`. Use an architectural decision record only
when the design document would become too broad or when several alternatives
need a durable comparison.
