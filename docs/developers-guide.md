# Developers' guide

## Spelling policy

Run `make spelling` to enforce en-GB-oxendict spelling. Typos scans tracked
Markdown, while the exact-phrase checker scans tracked UTF-8 text so prohibited
forms are also caught in source comments and tests. The tracked `typos.toml`
starts from the shared estate dictionary and applies the narrow repository
policy in `typos.local.toml`. Edit the local policy, then run
`make spelling-config-write` rather than changing generated entries by hand. Use
`make spelling-config` to validate that the generated file has not drifted.
The focused shared config builder refreshes its untracked dictionary cache only
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

## Workflow pins and Dependabot

Dependabot owns the upgrade of GitHub Actions and reusable workflows, including
calls into `leynos/shared-actions`. Contract tests that assert a caller's exact
commit SHA create a lockstep dependency: every time Dependabot opens a bump PR,
the test fails until a human edits the pinned constant to match. That defeats
the purpose of automated dependency updates and turns a routine bump into a
manual chore.

Contract tests may still verify the *shape* of a reusable-workflow caller. They
must not verify the specific SHA value.

- Do assert the workflow references the correct reusable workflow path.
- Do assert the ref is pinned to a full 40-character commit SHA, not a
  mutable branch such as `main` or `rolling`.
- Do assert the expected `on:` triggers, least-privilege `permissions:`, and
  the inputs the caller relies on.
- Do not hard-code the current SHA value as an expected string. Match it with
  a pattern instead.
- Do not fail a test purely because Dependabot bumped the pinned SHA.

```python
import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_uses_pinned_full_sha(caller_step):
    ref = caller_step["uses"].split("@")[-1]
    assert SHA_RE.match(ref), f"expected a 40-hex commit SHA, got {ref!r}"
```

If a workflow's behaviour genuinely depends on a feature only present from a
particular commit onwards, express that as a comment or a changelog note, not
as a test assertion on the SHA string.

## Mutation-testing workflow contract tests

This repository runs scheduled, informational mutation testing through a thin
caller workflow, [`.github/workflows/mutation-testing.yml`](../.github/workflows/mutation-testing.yml),
which delegates to the shared reusable workflow
`leynos/shared-actions/.github/workflows/mutation-mutmut.yml`. The heavy
lifting — running `mutmut`, and summarizing survivors — lives in
`shared-actions`; this repository carries only declarative configuration. The
run is **informational only**: it never gates a pull request. Survivors are
reported through the job summary and downloadable artefacts so they can be
triaged into tests, not enforced as a blocking check. The mutation targets and
test selection themselves are configured in `[tool.mutmut]` in
`pyproject.toml` (`source_paths`, `pytest_add_cli_args_test_selection`,
`do_not_mutate`).

The workflow runs in two modes. A **daily schedule** fires a change-scoped run
that mutates only the source files touched within the detection window, so
quiet days are cheap no-ops. A **manual dispatch** (the Actions "Run workflow"
control) mutates the whole package; select a branch in that control to
exercise a feature branch.

The caller passes a small set of configuration inputs, each carrying intent:

- `paths` — the change-detection glob (`rust_prover_tools/`) that decides
  whether a scheduled run has anything to mutate, bounding the scheduled run
  to real source changes.
- `module-prefix-strip` — set empty because the package uses a flat layout,
  so no path prefix needs stripping when translating a changed file into a
  mutation module glob.

The `uses:` reference pins the shared workflow to a full 40-character commit
SHA rather than a branch or tag, so a force-push upstream cannot silently
change what runs here. The contract test asserts only that the pin is a full
commit SHA, not a particular value, so Dependabot bumps it automatically
without any accompanying test edit.

Because the caller is configuration rather than code, `tests/test_workflow_contract.py`
pins the shape it must uphold, failing the pull request when the caller
drifts — repointing the pin at a branch, widening the token scope, or
dropping a configuration input — rather than letting the breakage surface
only in a scheduled run. The test module self-skips when the workflow file is
absent (mutmut copies the sources into a sandbox that omits `.github/`, so
the contract test does not run there). Run it locally with
`uv run pytest tests/test_workflow_contract.py -v`. The test validates:

- the `uses:` reference targets `mutation-mutmut.yml` pinned to a full commit
  SHA;
- the `with:` block carries exactly the expected `paths` and
  `module-prefix-strip` configuration;
- job permissions are least-privilege (`contents: read`, `id-token: write`)
  and the workflow-level default token scope is empty;
- `concurrency` serializes runs per ref without cancelling one in progress;
  and
- the triggers keep the daily schedule and a plain `workflow_dispatch` with
  no legacy branch input.
