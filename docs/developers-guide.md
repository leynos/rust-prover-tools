# Developers' guide

## Coverage publication

Main owns CodeScene. Pull-request CI measures `rust_prover_tools` serially with
`generate-coverage`, compares the result with the ratchet baseline that `main`
wrote (`with-ratchet: 'true'`), and uploads no artefact
(`publish-artefact: 'false'`). Nothing a pull request can start talks to
CodeScene or holds `CS_ACCESS_TOKEN`. The step is guarded to the `pull_request`
event, because `generate-coverage` saves its baseline on a push to `main` and
`coverage-main.yml` must be the only workflow writing it.

After each merge, `coverage-main.yml` regenerates the same measurement,
advances the ratchet, and uploads it with `upload-codescene-coverage` in
explicit `mode: upload`. A check step learns whether the token exists with the
one command
`echo "available=${{ secrets.CS_ACCESS_TOKEN != '' }}" >> "$GITHUB_OUTPUT"`,
which binds nothing, and the upload step runs only when that output is `true`
and `github.ref == 'refs/heads/main'`. The token reaches the uploader only as
its `access-token` input, because the uploader is a composite action and a step
`env` would reach every action nested in it. The ref guard means that a
`workflow_dispatch` aimed at a branch cannot publish that branch as `main`. Its
concurrency group never cancels, so no upload or baseline write is abandoned.
The group is keyed on `github.ref` alone, so runs on `main` never overlap, a
newer trigger replaces an older pending run rather than queueing behind it, and
a branch dispatch cannot displace a pending push to `main`. GitHub does not
promise to start runs in trigger order, so this is not a guarantee of commit
order: an older run can still publish last, and its coverage and baseline then
stand until a later successful run supersedes them; that is accepted. A
dispatch on `main` saves no baseline, because the baseline is saved only on a
push, so one that replaces pending pushes leaves the ratchet baseline behind
until the next push. A manual re-run of an older run keeps its SHA and its run
id: it republishes that commit's coverage to CodeScene, but its baseline cache
key already exists, so it replaces no baseline unless the original run saved
none. Merges made by the Dependabot automerge workflow's `GITHUB_TOKEN` fire no
push, so they are published only by a manual dispatch; this is a known
exception until the shared automerge workflow dispatches the publisher itself.
With no `CS_ACCESS_TOKEN` repository secret the upload skips; the ratchet
baseline is still written.

The retired `installer-checksum` input, the `CODESCENE_CLI_SHA256` variable,
and the `get-codescene-sha.yml` refresher are gone. The shared uploader selects
and verifies the `cs-coverage` archive from its own manifest.

`make test-workflow-contracts` holds this shape by running
`cv005-contracts check`, the shared contract library in `leynos/shared-actions`
(`packages/cv005-contracts`), from a full commit named by `CV005_CONTRACTS_REF`
in the Makefile, and CI runs it as its own step. A fix to the rules is
therefore a pin bump. The target needs `uv`, which fetches the Python 3.13 the
library runs under. The repository's one parameter is its `repository` name in
`.github/cv005.toml`. The library reads every workflow a pull request can start
(its own events, reviews, comments, the merge queue, `workflow_run` chains, and
pushes not confined to `main` or to tags), following local reusable-workflow
calls, and refuses any key or value in that closure naming the CodeScene host,
the credential, the client, or the uploader, and any read of the whole
`secrets` context or of a computed secret name. It holds the publisher and the
lanes to the rules above, and it reads workflows strictly, so a duplicate key
is refused rather than silently resolved. Its own suite proves each rule
refuses the shape it exists to refuse, so this repository keeps no copy of the
readers or the refusal cases.

Both coverage lanes set up Python 3.14 with `actions/setup-python`, inside the
project's `requires-python` (`>=3.14`). generate-coverage chooses its
interpreter from its `python-version` input, then `UV_PYTHON`, then
`.python-version`, then the `python3` on `PATH`, which is the most recent
`setup-python` step before the call in its job; `uv sync` refuses an
interpreter outside `requires-python`.
`tests/workflow_contracts/test_coverage_python_version.py`, with its reader in
`tests/workflow_contracts/coverage_python_sources.py`, requires every
generate-coverage call in the pull-request lane and the publisher to declare at
least one of those sources, every declared source to name the same version,
that version to be inside `requires-python`, and both lanes to measure on that
one version. A `setup-python` step guarded by `if:` or allowed to fail with
`continue-on-error` declares nothing. The ratchet baseline key already carries
the interpreter (`ratchet-baseline-<os>-py<major.minor>-`), so a lane on
another Python would miss its baseline rather than compare against the wrong
one; the contract turns that silent restart into a failure. It uses
`packaging`, a development dependency.

## Spelling policy

Run `make spelling` to enforce en-GB-oxendict spelling. It runs the pinned
`typos-config-builder gate`: Typos scans tracked Markdown, while the gate's
exact-phrase check scans tracked UTF-8 text so prohibited forms are also caught
in source comments and tests. The gate regenerates the tracked `typos.toml`
from the shared estate dictionary and the narrow repository policy in
`typos.local.toml`. Edit the local policy, then run `make spelling` and commit
the regenerated file rather than changing generated entries by hand. The
builder refreshes its untracked dictionary cache only when the authoritative
copy is newer. Bump the pin by changing `TYPOS_CONFIG_BUILDER_VERSION` in the
`Makefile`.

## Prover CLI internals

The `prover-tools` command is implemented as package code under
`rust_prover_tools/`. Keep command functions thin: they should bind Cyclopts
parameters, construct option dataclasses, call a feature module, and translate
expected `ProverToolError` exceptions to process exit codes.

`rust_prover_tools.cli` builds the Cyclopts applications and registers the
commands. A command whose parameter table is long enough to crowd that module
gets its own adapter module, which owns its parameter binding, environment
variables, output printing, and exit-code mapping:

- `rust_prover_tools.kani_exec_cli` for legacy Kani execution.
- `rust_prover_tools.verus_run_cli` for `verus run`. It binds `--proof-file`
  as a repeatable tuple, keeps `VERUS_PROOF_FILE` and `INPUT_PROOF_FILE` as
  single-file environment bindings, and supplies `env_var_split` so a path
  separator inside one value is not split into several proof files.

Feature modules should own workflow behaviour:

- `rust_prover_tools.kani` for Kani installation and version checks. The
  default route delegates to `rust_prover_tools.kani_binary`; `--compile`
  selects the source-build route in `kani` itself.
- `rust_prover_tools.kani_binary` for the verified prebuilt Kani install.
- `rust_prover_tools.z3` for the pinned Z3 solver install.
- `rust_prover_tools.verus.install` for Verus release installation.
- `rust_prover_tools.verus.run` for Verus binary resolution and toolchain
  checks.
- `rust_prover_tools.verus.batch` for the ordered, fail-fast proof-file loop,
  its partial-failure reporting, and its bounded telemetry.
- `rust_prover_tools.versions` for pure parsing and pin-file helpers.
- `rust_prover_tools.binary_assets` for pinned asset manifests.
- `rust_prover_tools.downloads` for digest pinning and the atomic verified
  store, with network policy in `rust_prover_tools.download_transport`.
- `rust_prover_tools.platforms` and `rust_prover_tools.path_resolution` for the
  shared host check and CLI path resolution.

When adding a pinned asset, update `binary_assets` with the exact URL, digest,
byte ceiling, and archive member, and add a test that builds a local archive of
the expected shape. Never let a download path skip `download_pinned`, and do
not extract archive members that are not named in the manifest.

Public helpers exported from `rust_prover_tools.verus` should keep structured
NumPy-style docstrings. That package is the stable import facade for tests and
future callers. The proof-run flow is split across three exported steps so each
can be tested alone: `resolve_proof_files` checks every requested path before
any verifier starts and falls back to the compatibility example path,
`ensure_run_binary` ensures the executable exists and installs it when
configured to, and `verify_proof_files` runs one invocation per file in the
caller's order and stops at the first non-zero exit.
`VerusRunOptions.proof_files` is the tuple that carries the request; the
deprecated `proof_file` alias is folded into it when a caller still uses the
former single-path form.

`verify_proof_files`, in `rust_prover_tools.verus.batch`, emits bounded INFO
events following the `kani_release` convention: a batch start and completion
event, and a start and completion event per invocation, so a stalled invocation
appears as an unmatched start. Only a genuinely stalled process leaves that
gap: every other exit path, including an exception outside the handled
failures, closes the invocation span as `failure` / `unexpected`. Completion
events carry the proof ordinal and count, status class, exit code, error
category, and duration; the batch completion adds the proved count. Those
fields are the metric surface for a log pipeline (batch size, invocation
outcome, and duration); no metrics client is installed. The clock is injectable
for deterministic tests. Events never carry proof paths or verifier output, and
the module installs no logger or handler, so the normal CLI stream is unchanged
unless the application enables logging.

`rust_prover_tools.exit_status` owns the closed-pipe boundary. `cli.main`
registers `guard_shutdown_flush()` before `app()` runs, so the interpreter's
final flush cannot replace the prover's status with 120, and it covers standard
error as well as standard output. Command functions must not install their own
`atexit` handling; use `exit_status.print_lines()` rather than bare `print()`
for output or diagnostics whose failure must not change the status. The design
rationale is in `docs/prover-tools-cli-design.md`.

Closed-pipe handling emits one opt-in INFO event per handled case when the host
application enables logging. The module installs no logger or handler and adds
no CLI output. Events contain only bounded fields: the stream (`stdout`,
`stderr`, or `custom`), the phase (`flush` or `write`), the action
(`redirected`, `sink`, `suppressed`, or `failed`), and the error category
(`broken_pipe` or `os_error`). They omit output, paths, commands and exception
text. A dead stream is replaced by a sink whenever it cannot be redirected,
including when the redirect itself fails, so no branch leaves the interpreter a
stream whose final flush would report 120.

`parse_verus_toolchain` accepts `Toolchain: <name>` lines at column zero or
with leading spaces or tabs. It extracts `<name>` before the trailing
`RUSTUP_TOOLCHAIN` override message emitted by official Verus releases, so
callers receive the toolchain identifier.

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

The project pins `cuprum==0.2.0b2` exactly. Cuprum 0.2 moved capture and echo
options from `run_sync` keyword arguments into `RunOutputOptions`, so
`run_command` passes
`output=RunOutputOptions(echo=..., broken_pipe_policy=...)` and
`context=ExecutionContext(...)`. Echo runs under
`BrokenPipePolicy.BEST_EFFORT`: the reader of an echoed stream is often a pipe
that closes early, and `STRICT`, the default, would fail a command that
succeeded. Do not raise the pin without reviewing
`docs/cuprum-v0-2-0-migration-guide.md` for breaking changes; the selection
contract reads the expected version from `pyproject.toml` and fails if any
dependency path disagrees.

`ExecutionContext.env` is an overlay on the parent environment, not a
replacement. A workflow that must remove inherited variables needs
`EnvMode.REPLACE`, which is imported from `cuprum.context` rather than from the
package root.

The two vendored guides under `docs/` are converted copies of the upstream
cuprum documents at the pinned tag, not this project's prose. Their wording is
upstream's; only links and line wrapping differ. Edit them only by re-running
the conversion against a new tag.

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
caller workflow,
[`.github/workflows/mutation-testing.yml`](../.github/workflows/mutation-testing.yml),
which delegates to the shared reusable workflow
`leynos/shared-actions/.github/workflows/mutation-mutmut.yml`. The heavy
lifting — running `mutmut`, and summarizing survivors — lives in
`shared-actions`; this repository carries only declarative configuration. The
run is **informational only**: it never gates a pull request. Survivors are
reported through the job summary and downloadable artefacts so they can be
triaged into tests, not enforced as a blocking check. The mutation targets and
test selection themselves are configured in `[tool.mutmut]` in `pyproject.toml`
(`source_paths`, `pytest_add_cli_args_test_selection`, `do_not_mutate`).

The workflow runs in two modes. A **daily schedule** fires a change-scoped run
that mutates only the source files touched within the detection window, so
quiet days are cheap no-ops. A **manual dispatch** (the Actions "Run workflow"
control) mutates the whole package; select a branch in that control to exercise
a feature branch.

The caller passes a small set of configuration inputs, each carrying intent:

- `paths` — the change-detection glob (`rust_prover_tools/`) that decides
  whether a scheduled run has anything to mutate, bounding the scheduled run to
  real source changes.
- `module-prefix-strip` — set empty because the package uses a flat layout,
  so no path prefix needs stripping when translating a changed file into a
  mutation module glob.
- `python-version` — the interpreter the shared workflow runs the project on,
  written as a non-empty quoted string (`"3.14"`). An omitted input takes the
  shared workflow's own default; an explicitly empty string does not, because
  GitHub Actions passes it through and only applies an input default when the
  caller omits the input
  ([actions/runner#2907](https://github.com/actions/runner/issues/2907)).
  setup-uv exports the selection as `UV_PYTHON` for the whole job, so `uv`
  syncs the project against it. The value must stay inside `requires-python` in
  `pyproject.toml`; a selection outside that range makes `uv` refuse the
  interpreter, and the run fails before collection or mutation.

At the pin this repository uses, the shared workflow defaults an omitted
`python-version` to `3.13`. That default suits a project baselined on 3.13, but
not this one, whose `requires-python` is `>=3.14`. **Omitting the input
therefore asks `uv` to sync this project on an interpreter the project
rejects**, which is the failure recorded in
[issue #172](https://github.com/leynos/rust-prover-tools/issues/172). For the
same reason the input is quoted: an unquoted `3.14` is a YAML float, and an
unquoted `3.10` reads back as `3.1`. Raise the caller's selection in the same
change as any bump to the `requires-python` floor; the contract test fails the
pull request if the two disagree. Making the shared workflow diagnose this
mismatch itself is tracked in
[shared-actions#591](https://github.com/leynos/shared-actions/issues/591).

The `uses:` reference pins the shared workflow to a full 40-character commit
SHA rather than a branch or tag, so a force-push upstream cannot silently
change what runs here. The contract test asserts only that the pin is a full
commit SHA, not a particular value, so Dependabot bumps it automatically
without any accompanying test edit.

Because the caller is configuration rather than code,
`tests/test_workflow_contract.py` pins the shape it must uphold, failing the
pull request when the caller drifts — repointing the pin at a branch, widening
the token scope, selecting an incompatible interpreter, or dropping a
configuration input — rather than letting the breakage surface only in a
scheduled run. The test module self-skips when the workflow file or
`pyproject.toml` is absent (mutmut copies the sources into a sandbox that omits
`.github/`, so the contract test does not run there). Run it locally with
`uv run pytest tests/test_workflow_contract.py -v`. The test validates:

- the `uses:` reference targets `mutation-mutmut.yml` pinned to a full commit
  SHA;
- the `with:` block carries exactly the expected `paths`,
  `module-prefix-strip`, and `python-version` configuration;
- the caller declares `python-version` explicitly as a non-empty string, and
  the declared version is inside `requires-python`; omission, an explicit empty
  value, an older version and an unquoted YAML float are all rejected, and
  synthetic in-memory callers prove that only omission resolves to the shared
  `3.13` default while an explicit empty value stays empty;
- job permissions are least-privilege (`contents: read`, `id-token: write`)
  and the workflow-level default token scope is empty;
- `concurrency` serializes runs per ref without cancelling one in progress;
  and
- the triggers keep the daily schedule and a plain `workflow_dispatch` with
  no legacy branch input.

The sandbox that mutmut builds also affects
`tests/workflow_contracts/test_coverage_python_version.py`. Its three
repository-backed tests — the ones that read `ci.yml`, `coverage-main.yml` and
`pyproject.toml` to hold the coverage lanes to one accepted interpreter — skip
when those files are absent, so the baseline mutmut runs before any mutant
neither fails on a `FileNotFoundError` nor aborts the run. The synthetic
selection and range tests need none of those files, so they keep running in the
sandbox.

Two further hazards come from mutmut's own trampoline rather than from missing
files, and both abort the run before any mutant is scored:

- The trampoline resolves the configured `source_paths` against the process
  working directory every time a mutated function is called. Tests that
  `monkeypatch.chdir` away from the sandbox root therefore fail under mutmut
  while passing in an ordinary run. `rust_prover_tools/unittests/conftest.py`
  defines `SKIP_CHDIR_UNDER_MUTMUT`, a `skipif` marker on `MUTANT_UNDER_TEST`,
  and the two affected tests carry it:
  `test_check_kani_version_resolves_relative_version_file_from_repo_root` in
  `test_kani.py` and
  `test_install_verus_resolves_relative_pin_files_from_repo_root` in
  `test_verus.py`. The marker is per test rather than per module, because both
  modules otherwise run fine in the sandbox and only these two prove that
  repository-relative paths resolve against the configured repository rather
  than the current directory.
- A test that patches `Path.resolve` globally breaks the trampoline's own
  bookkeeping, because the patched method is the one it calls. The symlink-loop
  boundary test therefore creates a real loop instead of monkeypatching the
  method, which also keeps the test closer to the failure it describes.

`rust_prover_tools/unittests/test_mutmut_sandbox.py` exercises the skip
predicate directly, so a lost guard fails an ordinary pull request rather than
waiting for the next scheduled mutation run.

## Running uv through the helper

Every `uv` call in the Makefile goes through `scripts/uv_gate.py`, a copy of
the helper in
[shared-actions](https://github.com/leynos/shared-actions/tree/main/uv_gate),
with one exception: the spelling baseline requires typos-config-builder at a
release tag to be called directly, so that call does not use the helper. It
cleans the environment, uses the global uv cache, runs offline first, and goes
online at most once, only when uv proves a cache entry, a managed Python or a
Git source is missing. The copy is vendored byte for byte and is never edited
in place: the concordat `uv-gate-baseline` rule audits its digest, so the
repository's file-length and scripting-stack conventions do not apply to it,
and Ruff excludes it. `make prepare` installs the locked environment, and every
gate that runs the project depends on it. A caller's `UV=/path/to/uv` is
honoured by putting that uv's directory on `PATH`, so the executable must be
named `uv`. A Git-sourced tool that is not cached yet needs uv 0.11.28 or
newer; with an older uv, running the tool once with `uvx` warms the cache.
`uv.lock` changes only through a manual `uv lock`; the gates never change it.

## Legacy Kani release boundary

`kani_release` owns explicit legacy execution, `kani_release_flags` normalizes
injected flags, and `kani_release_config` validates discovered Cargo inputs.
`kani_exec_cli` binds the public command without expanding the existing CLI
module. Keep installation and snapshot lifecycle code separate.

The SDK-child boundary removes only the exact bare `-C instrument-coverage`,
`--cfg=coverage` and `--cfg=coverage_nightly` atoms, in joined or paired forms.
Incomplete paired `--cfg` values fail before execution. Parameterized options,
valued or unrelated cfgs, compatible flags and `trybuild_no_target` remain in
order. A present `CARGO_ENCODED_RUSTFLAGS` wins over plain flags, including an
empty encoded value. Normalization preserves the parent mapping and configured
compiler/rustdoc admission wrappers, and ensures terminal `-D warnings` without
deduplicating earlier caller policy.

Cuprum applies an environment overlay. The child therefore receives equivalent
normalized plain and encoded flags; neither key is claimed to be removed. Kani
0.67 reads plain flags and synthesizes its own encoded target flags. Its target
uses the verifier backend; host compilation uses the compiler from the selected
release's matching toolchain. The child keeps Kani proxy hygiene: inherited
`toolchains/*/lib` entries are removed because they can load an incompatible
rustc driver; other inherited paths remain, after the selected matching runtime
directory. Preserving those competing driver directories would invalidate the
measured ABI boundary. The selected installation's `toolchain/bin` precedes
inherited `PATH`, so the driver's `cargo locate-project` uses Cargo from the
selected release's matching toolchain rather than an unrelated rustup shim. The
supported toolchain path may be a symlink into rustup's shared toolchain store.
Direct driver execution bypasses the release proxy's setup path. Kani 0.67
explicitly assigns its selected compiler for actual compilation; inherited
`RUSTC` does not override that assignment. Compiler identity queries retain
each configured wrapper in its original order; genuinely absent wrappers
require no substitute. Present empty or malformed tool selections fail before
compiler execution, as do unproved Cargo build/environment fallbacks. The
caller must keep the selected SDK assets and proof workspace manifests/Cargo
configuration unchanged throughout the invocation and nested Cargo children.
Cargo discovery requires non-empty `CARGO_HOME` or `HOME`; the implementation
uses its injected mapping and never falls back to ambient `Path.home()`.
Validation does not provide an atomic filesystem snapshot or protect against
concurrent replacement of SDK binaries/libraries or configuration. Build
scripts and procedural macros execute normally with caller permissions; this
route is not a sandbox for untrusted projects and makes no cryptographic
provenance claim. Never replace this boundary with global loader changes or
removal of an existing admission wrapper.

`kani_release` emits one bounded INFO event per completed stage when the host
application enables logging. It installs no logger or handler and adds no CLI
logging option. Events contain the stage, release, duration, status class and
error category; they omit paths, arguments, environment, output and exception
text. When at least one profile backend is pinned, the override reports a
`backend_overrides` stage event on the same logger before the enclosing
validation stage completes, adding the selection, variable and collision
counts. A repository that selects no backend emits no such event.

Backend classification is positional, not name-based. `kani_release_config`
walks every discovered Cargo configuration and ancestor manifest and returns a
`VerificationConfig` holding each accepted `profile.<name>.codegen-backend`
selection. `prepare_kani_command` then sets
`CARGO_PROFILE_<NAME>_CODEGEN_BACKEND=llvm` for exactly those profiles in the
child mapping. Cargo resolves that variable above both configuration files and
manifest profiles, which `tests/test_kani_exec_codegen_backend.py` measures
against the real bundled Cargo; the caller's mapping is never mutated. Only a
string profile selection is overridable: the boolean
`[unstable] codegen-backend` enable flag selects nothing, and a package-scoped
`profile.<name>.package.<package>` override wins over the variable, so it is
refused rather than silently ignored. Profile names are restricted to
`[A-Za-z0-9_-]+`, so the environment spelling is unambiguous. The opt-in
measurement module skips itself unless `PROVER_TOOLS_KANI_067_INSTALLATION`
names an installation root. Run it with:

```bash
PROVER_TOOLS_KANI_067_INSTALLATION="$HOME/.kani/kani-0.67.0" \
  uv run pytest tests/test_kani_exec_codegen_backend.py -v
```

That module carries two levels of evidence. The measured builds require a zero
status and the `llvm` backend for the accepted configuration-file and manifest
selections, and preserve an expected failure for the refused package override;
these pin the override mechanism. One acceptance case writes the exact
development configuration, adds a `#[kani::proof]` harness, and runs it through
the public CLI, which is the only evidence that such a repository completes a
proof. Keep the acceptance case and the measurement cases separate: Kani 0.67
supplies its own `-Zcodegen-backend` for the harness build and tolerates an
unknown name, so a successful harness run alone does not demonstrate the child
override.

Qualification must include a genuine dependency/build-script fixture after
shared gates. Controlled child tests establish invocation and failure
contracts, not full verifier compatibility. Linux x86_64 Kani 0.67.0 is the
bounded initial qualification target; an upstream release that drops wrappers
is unsupported.
