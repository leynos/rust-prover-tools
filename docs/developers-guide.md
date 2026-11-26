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
- `rust_prover_tools.kani_snapshot_cli` for `kani snapshot install`,
  `kani snapshot check` and `kani run`. It binds the pin, cache and asset
  directories and keeps every argument after `--` as a forwarded verifier
  argument.
- `rust_prover_tools.verus_run_cli` for `verus run`. It binds `--proof-file`
  as a repeatable tuple, keeps `VERUS_PROOF_FILE` and `INPUT_PROOF_FILE` as
  single-file environment bindings, and supplies `env_var_split` so a path
  separator inside one value is not split into several proof files. It also
  binds `--z3` and `VERUS_Z3_PATH`, probes and qualifies that executable with
  `probe_and_qualify_z3`, refuses an extra argument that selects CVC5 beside a
  Z3 selection, and forwards the qualified path to `run_verus` as
  `VERUS_Z3_PATH`.
- `rust_prover_tools.kani_log_cli` for `kani analyse-log`. It disables inherited
  `INPUT_*` configuration and maps unreadable inputs to status `1` and a
  completed non-passing analysis to status `2`.
- `rust_prover_tools.verus_log_cli` for `verus analyse-log`. It applies the
  same input-and-status mapping, resolves the log's release from
  `--verus-release` or the consumer's `tools/verus/VERSION` pin and refuses one
  the parser is not qualified against, parses `--expect-failure` into a
  `FailureExpectation`, and renders either text or the schema-versioned JSON
  object.
- `rust_prover_tools.trust_audit_cli` for `audit trust`. It maps arguments,
  resolves the repository root, selects the prover vocabularies named by
  `--prover` (both by default), and renders either the item list or the
  schema-versioned JSON object. Driver and target discovery belong to the
  `trust_audit` package, so the adapter holds no Cargo rules. A refused source
  or an unknown prover is status `1`; a completed audit is `0` however many
  items it found, because the audit keeps no record of which requests were
  justified.

Feature modules should own workflow behaviour:

- `rust_prover_tools.kani` for Kani installation and version checks. The
  default route delegates to `rust_prover_tools.kani_binary`; `--compile`
  selects the source-build route in `kani` itself.
- `rust_prover_tools.kani_binary` for the verified prebuilt Kani install.
- `rust_prover_tools.kani_archive` for extracting and transactionally
  publishing the Kani frontend executables.
- `rust_prover_tools.z3` for the pinned Z3 solver install.
- `rust_prover_tools.verus.install` for Verus release installation.
- `rust_prover_tools.verus.run` for Verus binary resolution and toolchain
  checks.
- `rust_prover_tools.verus.batch` for the ordered, fail-fast proof-file loop,
  its partial-failure reporting, and its bounded telemetry.
- `rust_prover_tools.kani_log` for Kani transcript parsing and manifest-bound
  decisions. `syntax`, `model`, `reader`, `reports` and `extract` own the
  grammar and extraction; `manifest` validates the expected harness set;
  `observation` reduces one block to the facts a decision reads; and `decide`
  applies the outcome precedence.
- `rust_prover_tools.verus_log` for Verus transcript parsing and decisions.
  `syntax` owns the grammar and the releases it was qualified against, `model`
  holds the parsed facts, `reader` scans a merged capture, `expectation` reads
  a negative-control region, `decide` maps the facts to an outcome and run
  mode, and `checks` holds a run to the counts, failure region and exit status
  a qualification caller declared.
- `rust_prover_tools.trust_audit` for the Rust trust audit. `vocabulary`
  defines one frozen record per prover and selects the records a caller asks
  for; `syntax` lexes Rust so trivia is never read as code; `structure` and
  `conditions` locate brackets and read conditional attributes; `macro_rules`
  collects definitions and invocation sites; `module_paths` resolves a
  declaration to a file; `inline_modules` finds the enclosing inline chain and
  the path attribute on a declaration; `use_tree`, `use_paths`,
  `crate_bindings`, `use_bindings`, `manifests` and `modules` locate imports,
  crate kinds and module declarations; `context` applies the rules; `matcher`
  refuses code it cannot see and exposes `scan`; `tree` walks the crate from a
  set of entry points; `drivers` expands command arguments into Cargo target
  pathnames; `targets` applies Cargo's own target discovery and `auto*` flags;
  and `manifests` reads each crate's kind with the one `read_cargo_manifest`
  decoder both target discovery and proc-macro detection share. `module_paths`
  probes the host paths a declaration names. `drivers`, `targets`, `manifests`,
  `module_paths` and `tree` are the package's I/O edge; every other module is a
  pure function of the text it is given.
- `rust_prover_tools.z3_selection` for resolving a solver selection and
  qualifying an observed version. Resolution reads the filesystem and `PATH`;
  the `--version` probe lives in `verus_run_cli`, which also refuses an extra
  argument that would override a selected Z3 with CVC5.
- `rust_prover_tools.versions` for pure parsing and pin-file helpers.
- `rust_prover_tools.binary_assets` for pinned asset manifests.
- `rust_prover_tools.downloads` for digest pinning and the atomic verified
  store, with HTTPS URL policy in `rust_prover_tools.download_url`, connection
  and redirect policy in `rust_prover_tools.download_transport`, body limits in
  `rust_prover_tools.download_stream`, and the shared deadline budget and
  watchdog in `rust_prover_tools.download_deadline`.
- `rust_prover_tools.platforms` and `rust_prover_tools.path_resolution` for the
  shared host check and CLI path resolution.
- `rust_prover_tools.observability` for the shared opt-in event helper.

Installer and download boundaries report through
`log_event(logger, event, **fields)` in `rust_prover_tools.observability`. The
helper emits one DEBUG record whose payload is an `event_data` mapping. Event
names are stable identifiers such as `download.start` or
`kani.install.complete`, and their fields stay bounded to counts, durations,
closed-set category strings such as phase names, and log-only identifiers such
as a pinned asset name. Durations and counts are metric observations rather
than labels; a numeric label is safe only when its values form a small, bounded
set, and a unique value such as `span_id` or a caller-supplied asset name must
never become one. Never include URLs, filesystem paths, credentials, digests,
or payloads. The library installs no handlers, so an application opts in by
lowering the level of the relevant `rust_prover_tools` module logger.
`docs/prover-tools-cli-design.md` owns the system-design record for the event
vocabulary.

The producer entrypoint is the one place that opts in. `_configure_logging` in
`rust_prover_tools.kani_snapshot.orchestrator.entrypoint` attaches a standard
error handler to the `rust_prover_tools` logger at DEBUG before dispatch, and
its formatter renders each event's `event_data` beside the event name as
`key=value` pairs. A hosted job therefore shows the decision sequence, the
transient retries and the operation's ending without a local rerun; the handler
belongs to the CLI entrypoint, not to a library import, so embedding the
package does not reconfigure a host application's logging. The events a
publication job emits are `kani.snapshot.publish.start`, `.decision` (`step`,
`action`), `.deferred` (`step`), `.transient` (`action`), and `.complete` or
`.failed` (`span_id`, `outcome`, `elapsed_seconds`, and `reason` on failure);
the `gh` transport adds `kani.snapshot.gh.failed` (`kind`, and `exit_code` for
a command that exited) and `kani.snapshot.gh.absent` (`exit_code`). A timed-out
command never produced an exit status, so its `kani.snapshot.gh.failed` event
carries `kind=timeout` and no `exit_code`: a consumer must treat that field as
present only for a failure that had one. A failure that carried a decided
outcome reports it, so a budget that ran out reads `outcome=not_converged`
rather than only the exception's class.

A process or storage boundary that should be timed uses
`operation_span(logger, event, **fields)` from the same module. It emits
`event.start` and a matching `event.complete` or `event.failed`; the closing
event repeats the start fields, adds `elapsed_seconds`, and, on success, the
bounded outcome recorded by `span.finish(outcome, **fields)`. A failure names
only the exception's class as `reason`, because an exception message can embed
a filesystem path.

When adding a pinned asset, update `binary_assets` with the exact URL, digest,
byte ceiling, and archive member, and add a test that builds a local archive of
the expected shape. Never let a download path skip `download_pinned`, and do
not extract archive members that are not named in the manifest. The one bounded
exception is the installer's evidence fetch: `downloads.BoundedAsset` names a
release file with a size ceiling and no pre-known digest, and
`downloads.download_unverified` transfers the two self-authenticating evidence
files that fit it. The digest it returns is an observation, not a pinned check:
the call decides nothing about trust, and it does not replace `download_pinned`
for anything the manifest pins.

`UrlPolicy` receives two separate checks per download: `check_initial` sees the
first URL and `check_redirect` sees every redirect target before it is opened,
so a redirect cannot relax the policy. The snapshot implementation,
`ReleaseUrlPolicy`, requires the initial URL to be the exact release URL for
the pinned repository, tag and asset, with no query. Every redirect must use
HTTPS, stay on a documented GitHub asset host, use the default HTTPS port, and
carry no credentials or fragment, and its path must be contained.
`download_asset` retries only `TransientDownloadError`, with a bounded attempt
count and capped exponential backoff; storage, integrity and policy failures
are reported after one attempt.

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

### Download deadline

One monotonic deadline, created in `download_transport._download` before the
first connection, covers every redirect hop. `download_deadline` owns the
budget and the enforcement:

- `_hop_timeout` reads the clock once, refuses a hop with no time left, and
  otherwise returns the smaller of the hop limit and the time remaining. One
  reading is deliberate: a guard and a separate budget reading could straddle
  the deadline and hand the socket a zero timeout, which makes it non-blocking
  instead of failing.
- `_open` connects explicitly rather than letting `request()` connect lazily,
  then calls `_arm_socket` so the send runs under the time the connect phase
  and TLS handshake left. A socket timeout bounds one blocking call, not a
  sequence of them, so without the re-arm a hop could spend a fresh budget on
  the send after already spending one connecting.
- `_DeadlineWatchdog` captures the socket when it is armed and shuts it down
  at the deadline from a daemon thread. The capture happens before
  `getresponse()`, which clears `connection.sock` for a response the connection
  cannot reuse. An unbounded deadline starts no thread at all.
- `_HopHandle.close()` stops the watchdog and then closes the connection, in
  that order and under `try`/`finally`, so a finished hop cannot shut down a
  later hop's socket and a failed stop cannot leak the connection.

Connect, re-arm, send and response acquisition share one `try`. Any failure
stops a started watchdog and closes the connection before re-raising. An
`OSError` or `HTTPException` is converted to the base deadline error, with a
`deadline` event, when the watchdog expired or the clock has reached the
deadline; otherwise it stays a transient `transport_error` so a bounded retry
can still run. `download_stream._copy_body` applies the same rule to a body
read, including an EOF at or after the deadline, because a socket the watchdog
shut down reads as end of stream.

The deadline is not a hard wall-clock bound. DNS resolution has no socket
timeout; the standard library applies one connect budget to every resolved
address in turn, and the initial TLS handshake, which `connect()` performs
before the watchdog is armed, reuses that budget. Local file I/O while the body
is written is not supervised either. Digest verification runs after `_download`
returns and is bounded only by the byte ceiling already enforced on the
download; publication has no time bound at all; and a retry is bounded by its
attempt count and backoff rather than by the transfer's deadline.

## External commands

Do not call `subprocess`, shell strings, or Plumbum from new prover workflows.
Use `rust_prover_tools.commands.CommandSpec` with `run_command` or
`run_checked`.

Build command arguments as tuples of strings. Parse user-provided command
strings with `split_command`; it uses `shlex.split` and rejects empty commands.

Use `run_checked` when non-zero exits should fail the workflow immediately with
a user-facing `CommandFailedError`. Use `run_command` when the workflow needs
to inspect the exit code, such as `rustup which` or `verus --version`.

A command that exceeds its wall-clock budget is terminated, process group and
all, and `run_command` raises `rust_prover_tools.commands.CommandTimeoutError`.
That class subclasses `ProverToolError`, so existing handling keeps working,
but a caller that may retry a timeout should catch the narrower type, because a
genuine command failure is not worth repeating. The release path does this at
two boundaries: `gh_transport` catches `CommandTimeoutError` and wraps it once
in `ReleaseTransientError`, and the publication loop catches that wrapper. A
caller adding retry handling to the release path therefore catches
`ReleaseTransientError`, not the timeout type, which never reaches it.

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
replacement. `CommandSpec` therefore accepts `env` values that are either a
string or `cuprum.UNSET`: under the default `EnvMode.OVERLAY`, an `UNSET`
marker removes that variable from the child without mutating this process's
environment. `EnvMode.REPLACE` instead starts the child from an empty
environment, so it discards every inherited variable except those listed.
`EnvMode` is imported from `cuprum.context` rather than from the package root.

`CommandSpec.capture` stores stdout and stderr on the returned `CommandResult`.
Set it to `False` for output whose size the caller does not control, because a
verbose verifier would otherwise size the CLI's memory. `on_line` accepts a
`cuprum.LineHook` called once per decoded line, independently of `capture`. For
a long stream, pair `capture=False` with a bounded collector such as
`TailCapture`, so the hook sees every line while only a fixed byte budget is
retained.

`timeout_seconds` is the wall-clock budget for the whole child process tree.
`None` leaves the command unbounded unless an enclosing Cuprum scope supplies a
default, which `run_command` resolves before deciding how to launch the child.
Whenever an effective timeout applies, the child is placed in its own process
group, so expiry terminates every descendant it started rather than leaving a
setup tool to outlive the caller's budget.

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
  `test_verus_install.py`. The marker is per test rather than per module,
  because both modules otherwise run fine in the sandbox and only these two
  prove that repository-relative paths resolve against the configured
  repository rather than the current directory.
- A test that patches `Path.resolve` globally breaks the trampoline's own
  bookkeeping, because the patched method is the one it calls. The symlink-loop
  boundary test therefore creates a real loop instead of monkeypatching the
  method, which also keeps the test closer to the failure it describes.

`rust_prover_tools/unittests/test_mutmut_sandbox.py` exercises the skip
predicate directly, so a lost guard fails an ordinary pull request rather than
waiting for the next scheduled mutation run.

## Kani corpus and lint exceptions

The Kani parser corpus lives under versioned directories such as
`tests/fixtures/kani_logs/0.67.0/`. The repository's global `*.log` ignore
would otherwise drop it, so `.gitignore` carries an explicit
`!tests/fixtures/kani_logs/**/*.log` exception (and the matching one for
`verus_logs`). A new capture must be added under a version directory, or the
exception extended, before its test can rely on it. A capture that exists only
in a working tree passes locally and fails a clean CI checkout.

`pyproject.toml` relaxes two per-file lint rules for this capability, and both
are narrower than they look:

- `rust_prover_tools/kani_harness_cli.py` keeps
  `too-many-arguments` and `too-many-positional-arguments` disabled because
  Cyclopts evaluates its command annotations; the option groups themselves live
  in `rust_prover_tools/kani_harness_cli_options.py`, which is not exempt.
- `rust_prover_tools/unittests/*_support.py` and `tests/*_support.py` keep the
  test-only relaxations for assertions, subprocess use, argument counts, local
  counts, magic values and `no-self-use`. A new shared test helper belongs in
  one of those directories with the `_support.py` suffix; a production module
  named `*_support.py` gains none of these relaxations.

## Docstring coverage

`make lint-python` depends on `build` and on `make interrogate`, which needs
`uv` and fails on any Python definition it scans under `rust_prover_tools`,
`tests` and `scripts` without a docstring, and on any `MISSED` row. The
configured exclusions are the recognized `@overload` stubs (a docstring on each
is forbidden by Ruff) and the name `getheader`, held by the contract test to
its overloads: every non-overload definition of that name must carry a
docstring. `scripts/uv_gate.py` is the vendored shared-actions helper; it is
not edited here, and it ships with complete docstrings. Ruff checks docstring
style for `PYTHON_TARGETS` only, so `scripts` is held to presence, not style.
Add a new Python directory to `INTERROGATE_TARGETS`;
`tests/test_interrogate_gate.py` asserts the wiring and runs the real pinned
tool over a generated documented and undocumented script.

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

## Kani log analysis and corpus

The Kani log grammar is read from real transcripts, not from Kani's
documentation. Each supported release therefore has its own corpus under
`tests/fixtures/kani_logs/<kani-version>/`, and every parser and decision fact
is asserted against that corpus.

- The logs are corpus evidence: recapture them, never hand-edit them.
- Capture the complete standard output followed by the complete standard
  error. Merging the streams can split a report block.
- Register a new release in three places at once: add its `KaniGrammar` to
  the registry in `rust_prover_tools/kani_cli_grammar.py` (with `KANI_067` as
  the model), add the version-named corpus directory under
  `tests/fixtures/kani_logs/`, and add the release to `SUPPORTED_KANI_VERSIONS`
  in `rust_prover_tools/kani_log`. Delete all three when the release leaves the
  supported set. The registry's import-time check refuses a process whose
  registered set differs from `SUPPORTED_KANI_VERSIONS`.
- `expected.json` records the facts each log must yield and the decision it
  must reach. `decision_table.json` holds the precedence table as data, and the
  manifest files declare the harnesses the corpus run selected.
- `crate/` holds the corpus crate sources, `Cargo.lock`, `harnesses.json` and
  the captured `kani-list.json` so a maintainer can recapture the logs.
  `kani-list.json` is an inventory only and cannot supply roles, covers,
  expected failures or `should_panic`.
- The README records the release, Rust toolchain, capture route, every
  command and exit status, and the origin commit. It carries no digest
  provenance: the transcripts themselves are the qualified evidence.

A rendering rule that no captured log can settle is read from the pinned
release's own driver source instead of guessed: how a description holding a
newline is printed, how the echoed solver command is quoted, and which verdict
can carry the expected-panic note. The corpus then keeps a log that exercises
the rule, so the parser and the decision stay tied to evidence.

`rust_prover_tools/unittests/test_kani_log_parse.py` loads every corpus
directory whose name is in `SUPPORTED_KANI_VERSIONS`; a supported release
without a directory fails that test. The decision tests read the same corpus,
so a parser change and a decision change cannot drift apart while both remain
green.

The shared test helpers are split by concern so each module stays reviewable:

- `tests/kani_log_corpus.py` reads the committed logs, `expected.json`, the
  decision table and the manifests.
- `tests/kani_log_fixtures.py` builds the log and manifest one synthetic fact
  combination describes.
- `tests/kani_log_combinations.py` enumerates the combinations the exhaustive
  checks range over. A variant added there widens every property stated over it.
- `tests/kani_log_properties.py` states the properties themselves.

## Verus log analysis and corpus

The Verus grammar is likewise read from captured transcripts. Each supported
release has its own corpus under `tests/fixtures/verus_logs/<verus-release>/`,
and the corpus directory must exist before the release joins
`SUPPORTED_VERUS_VERSIONS`.

- Recapture a transcript rather than hand-editing it, and keep the complete
  output of one invocation.
- `tests/verus_log_corpus.py` reads the committed logs, their expected facts
  and the negative-control expectations the tests assert against.
- The release named by `--verus-release`, or by the consumer's
  `tools/verus/VERSION` pin, is checked against the supported set. When neither
  names a release, analysis proceeds with an unnamed release rather than
  guessing one.
- A run that is expected to fail names a region (`path[:start[-end]]`) with
  `--expect-failure`; exactly one located failure must land inside it.

## Kani snapshot installation and execution

`rust_prover_tools.kani_snapshot.install` orchestrates one installation: stage
and verify transport files, extract the launcher, run setup, probe the
installed compiler, write the receipt, then publish the next activation record.
`install_sender` owns where those files come from: `ReleaseSender` downloads
the release the reviewed pin names, pinning the manifest to the pin's digest
and each payload to its manifest record, while `LocalSender` copies from
`--asset-dir` under the same envelopes. Neither decides trust; both bound and
verify bytes, and `install_transport` keeps the order in which the manifest,
checksums, attestation and payloads may be trusted. `install_state` owns
locking and cache revalidation, `activation` owns the ordered create-only
records, `completion` owns the per-identity completion watermark those records
advance, `durability` owns the flushes that make state durable, and `cleanup`
owns bounded removal of private, uniquely named state. `run` only revalidates
and executes; it never installs. `kani_snapshot_cli` registers the public
commands, and `cli_support` holds the repository-root and error-exit helpers,
plus `read_input_text` and `InputError`, the strict UTF-8 reader the
log-analysis commands use so an unreadable or non-UTF-8 input becomes status
`1` with a diagnostic naming the input. Keep lifecycle code out of the legacy
`kani_release` modules.

The dependency direction is CLI to orchestration to validated models and
command adapters. `InstallHooks` is the injection seam for setup, probe and
attestation, which keeps deterministic tests off the network and out of the
host toolchain. Production defaults live in `install_defaults.py`: setup runs
the packaged `cargo-kani` from the extracted launcher with
`execution_environment`, and the probe reads the absolute toolchain link before
executing `rustc -vV` and `kani-compiler --version`.

`attestation.py` owns the attestation boundary, beside the manifest, payload
and checksum checks in `integrity.py`, and `workflow_run.py` owns its
run-correlation half. `verify_attestation` runs `gh attestation verify` under
the fixed policy flags, parses only verified records, and requires at least one
of them to name the manifest's own run. That correlation compares the SLSA v1
`runDetails.metadata.invocationId` with
`https://github.com/<repository>/actions/runs/<run_id>/attempts/<run_attempt>`
built from the policy repository and the manifest's `WorkflowIdentity`, so an
attestation for another run, or for another attempt of the same run,
authenticates nothing this installation may use. Policy, artefact, bundle and
identity travel together as one `AttestationRequest` through
`InstallHooks.verify_attestation`, so a test double observes exactly the run
the manifest records rather than an assumption about it. The tests exercise
each policy dimension as a refusal: a shared verifier model holds the identity
a modelled bundle claims, compares it with the trusted flags the command built,
and accepts a matching bundle, so a wrong signer workflow, source ref, source
digest or signer digest is refused rather than masked. An installation test
runs the real verifier against a bundle that fails one dimension at a time and
requires launcher extraction and setup never to be reached, because the
launcher is extracted before setup; a matching-bundle control requires both to
be reached, so the spies cannot pass vacuously.

Ordering is part of the contract. The manifest digest and pin binding are
checked before checksums, checksums before attestation and payload copies, and
payload digests before archive structure. `validate_archive_inventory` then
requires two-way path equality and equal kind, mode, size and link target.
Because that comparison includes the member kind, the producer builds the
inventory with the archive's headers beside the extracted tree: extraction
makes every name in a hard-link group the same inode, so only a header can say
which name carries the bytes, and a walk order that disagreed with it would
describe the same tree under a designation the consumer refuses. Setup runs
only after those checks, and the active pointer moves only after the receipt is
fsynced and the probes match the manifest. Immediately after setup,
`python_root.record_python_root` writes `pyroot.json` beside the receipt, and
`validate_installed_tree` requires the tree to match it exactly; that closes
the inventory's open-ended `pyroot` exemption, because execution puts the tree
on `PYTHONPATH` and `pyroot/bin` on `PATH`. Activation also runs the same
runtime-file contract as `check` before probing: launchers must be present and
executable, `pyroot` a real directory whose resolved links stay inside the
Python root itself (the record describes link text, not reachable content), the
rustup home itself must resolve inside the generation, and the toolchain's
recorded shared libraries must be covered with no unrecorded `.so` beside them
inside that home. Before publishing the activation record the installer flushes
every entry that contract requires: the launchers, driver, compiler and
`rustc`, the toolchain component record, every recorded library, every file and
directory the backend inventory publishes, the directories that name them
(including each required toolchain file's whole ancestor chain, so
`lib/rustlib` is flushed even without a recorded library beneath it), and every
member of the setup-created Python root. Files are flushed before their
directories, so a crash cannot leave a durable record naming bytes that were
never written back. A successful activation then advances the identity's
completion watermark, the highest of one small create-only record per completed
installation, named by its sequence and written only after the record's flush
succeeds. Such a record names an installation that finished rather than one
that merely published a record, and a record that lands after a newer one joins
the order beneath it instead of replacing it, so the watermark never regresses.
Only a canonical record counts: a regular file named by its 20-digit sequence
whose contents name that same sequence, read only as far as a canonical record
so oversized evidence cannot exhaust the process. Anything else names no
finished installation and cannot retire a marker. Each publication marker
records its base: the highest activation record its publication observed before
replacing anything. Readers refuse a marker until the watermark rises above
that base, and ignore one a later completion has passed. Because an
installation links its record before recording completion, a completion above a
base always belongs to an installation that started after the publication,
while a completion whose record the publication already saw cannot retire the
marker even when its write lands late; a marker that records no base, whose
content cannot be parsed, whose bytes are not exactly what the writer produces
for the base it appears to name, or whose base carries more digits than an
activation sequence, is still pending, and an absent watermark, or evidence
that is malformed, unreadable or mismatched, reads as zero. A parsed base is
accepted only from the canonical spelling of the whole file, so a leading zero
names no base. A marker is read only as far as the longest canonical one plus a
byte, so a corrupt marker can neither exhaust the process nor raise out of
`check`, and a marker that fills that read is refused rather than trusted for
its visible prefix; the next install repairs the cache because the pending
marker sends it down the publication path, which clears stale markers. Ordering
against completions rather than the activation order matters because an
unrelated late activation can outrank the record a publication observed without
that publication having finished. Because reuse reissues the record it
validated and then records its own completion above every marker's base, an
abandoned publication that writes its marker after a reuse names a lower base
and cannot make a completed cache look unfinished. A stale publication marker
whose removal fails is an installation failure rather than a logged deferral,
because readers still refuse a cache whose marker has not been superseded.
Cache reuse rehashes the backend, inventory and launcher payloads and re-reads
each archive's structure, so a size-preserving edit is still refused. A failure
must leave the previous pointer and generation intact.

Proof execution mirrors the launcher's streams live and retains only a bounded
tail of each one through `commands.TailCapture` (64 KiB per stream by default).
The capture ceiling is independent of `CommandSpec.capture`, so `run` never
buffers a whole proof log in memory while still returning the final lines that
explain a failure.

Cache reuse is revalidation, not trust in directory existence.
`validate_generation` reloads the receipt, checks cached sizes, recomputes the
digests of the inventory and launcher, re-reads the backend archive against the
inventory, requires the runtime executables and absolute toolchain link, and
re-runs the probes. The installation budget is passed as a checkpoint callable
through staging, hashing, extraction, tree validation and toolchain
verification as well as setup and the probes, so no long local phase can
outlive the deadline. Filesystem operations that may block on a hung filesystem
— creating the identity directory, opening the lock file, private directory
creation and removal, the staged manifest and cached-inventory reads, each
flush, the runtime-file checks that precede the probe, and the
activation-record listing that allocates the next sequence number — are
individually supervised, so none of them can hold the identity lock past the
deadline. The lock wait is bounded by the remaining budget rather than only by
`lock_timeout_seconds`, so time spent before the wait cannot extend the call
past its deadline. A corrupt generation is repaired by installing a new one; no
code path deletes a previous generation.

`run` executes the identity returned by `check_snapshot` rather than resolving
the activation records again, so an active generation changed between
validation and execution cannot redirect a proof. The probe and the proof both
route dynamic libraries through the verified toolchain's `lib` directory, and a
pre-existing `LD_LIBRARY_PATH` entry under another Rustup toolchain is dropped
rather than inherited.

The snapshot CLI uses `config=()` on its sub-applications so Cyclopts does not
inherit generic `INPUT_*` bindings. `run` must keep `Path` as a runtime import
because Cyclopts evaluates annotations when it builds the command. The
attestation verifier is the bare executable name `gh`, resolved through the
caller's `PATH`; tests put a `gh` stub first on `PATH` for behavioural runs.
Production therefore trusts whatever `gh` the caller's `PATH` selects, and a
`PATH` entry under an untrusted directory can replace the verifier. Run
snapshot installation with a `PATH` that resolves `gh` to the reviewed GitHub
CLI installation.

Test layers are deliberate. Unit tests in
`rust_prover_tools/unittests/test_kani_snapshot_install.py` cover activation,
reuse, each rejection edge and repair with injected hooks.
`tests/kani_snapshot_bundles.py` builds complete local releases, including a
working `cargo-kani setup` stub, so `tests/steps/test_kani_snapshot_steps.py`
can install through the real CLI and then run the pinned launcher.
`tests/test_kani_snapshot_activation.py` drives two identities through a
Hypothesis rule-based state machine over the real install pipeline,
`tests/test_kani_snapshot_publication_model.py` drives interrupted and late
marker, activation and completion writes through the real publication,
activation and completion readers, and
`tests/test_kani_snapshot_download_e2e.py` downloads through a real loopback
HTTPS server so the TLS handshake, redirect policy, retry loop and digest check
are not replaced. `tests/test_kani_snapshot_concurrency.py` uses real processes
for the advisory lock, overlapping installs, a killed owner and a bounded
timeout, and `tests/test_kani_snapshot_interruption.py` kills an installer
mid-staging and seeds a setup fault while a peer waits.
`rust_prover_tools/unittests/test_kani_snapshot_archive.py` adds generated
member graphs checked against an independent expected-tree oracle. Keep these
fixtures structural: they must exercise the production parsers rather than
bypass them.

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

## Kani supervised harness execution

The supervised layer answers a different question from installation and
single-invocation execution: which exact harness is running, how much memory
and time it is using, and why it stopped. Its modules divide that question by
responsibility rather than by prover:

- `rust_prover_tools.kani_cli_grammar` is the single registry of Kani
  release grammars. `KANI_067` is the qualified 0.67.0 entry, `grammar_for`
  resolves a release, `SUPPORTED_VERSIONS` lists the registered set, and
  `UnsupportedKaniGrammarError` refuses an unregistered one. An import-time
  check ties the registry to `SUPPORTED_KANI_VERSIONS` from `kani_log`, so a
  release cannot be half-registered.
- `rust_prover_tools.kani_log` is the shared 0.67.0 grammar, report model,
  reader, and reconciliation. Its grammar, model, reader, and corpus derive
  from Combobulate at commit `9dd6e3c`; see
  `rust_prover_tools/kani_log/ATTRIBUTION.md`. `parse_kani_log` parses whole
  text and `parse_kani_log_lines` drives the same engine from a bounded line
  source, so the live and offline paths cannot drift.
- `rust_prover_tools.kani_inventory` resolves Cargo packages and targets,
  scans Rust sources lexically for proofs, follows module declarations and
  `#[path]`, and reads or writes the strict version-1 inventory document.
  `select` chooses exact harnesses and composes the verifier argv.
- `rust_prover_tools.kani_routes` defines `LegacyRoute`, `SnapshotRoute`, and
  the `PreparedRoute` seam. Every harness command is derived from one prepared
  route, so identity probes and the environment are not rebuilt per harness.
- `rust_prover_tools.kani_qualification` validates one release installation,
  builds its child environment, probes the compiler and verifier identities
  once, and derives every harness command from that base. The legacy
  `exec_kani` entry point keeps its stage order and delegates here.
- `rust_prover_tools.kani_snapshot.run` exposes `prepare_snapshot_proof` and
  `QualifiedSnapshotProof` so a supervised snapshot run reads one activation
  and builds one proof environment.
- `rust_prover_tools.supervision` is prover-neutral. Its protocols module
  names every injectable seam: clocks, launchers, resource probes, cgroup
  readers, interrupt sources, sleepers, and evidence sinks. `RunEnvironment`
  binds them for one run, and `rust_prover_tools.kani_harness_models.py` holds
  the options and dependency overrides a consumer sets. The tests substitute
  each through those protocols rather than patching the runner.
- `rust_prover_tools.supervision.sinks` owns the human and JSONL sinks;
  `schema_fields`, `schema_values`, `schema_records`, and `schema` own the
  strict JSONL and report contracts; `report` publishes the final document.
- `rust_prover_tools.supervision.kani` and
  `rust_prover_tools.supervision.kani_outcome` are the Kani adapter and its
  outcome construction. They live in the supervision package because the
  adapter seam is small and prover-neutral; a second prover adds a sibling
  module rather than another package.
- `rust_prover_tools.supervision.kani_observer` is `KaniObserver` and
  `KaniLiveFacts`. The observer records live selection and progress facts and
  spools the report; it never attributes a proof verdict, because description
  prose can contain a verdict-shaped line. The parse supplies the verdict.
- `rust_prover_tools.kani_harness_run` is the composition root. It loads or
  discovers the inventory, selects exact identities, qualifies one route,
  composes and validates each argv, runs the supervisor, and returns a
  `KaniHarnessRun`.
- `rust_prover_tools.supervision.destinations` resolves evidence destinations
  and refuses two roles that name one file, including symlink and hard-link
  aliases. The rule needs filesystem identity, so it lives beside the adapters
  and never inside the policy value object.
- `rust_prover_tools.kani_diagnostics` opens the configured human, JSONL, and
  report destinations for a library run, applying that rule before the first
  handle exists.
- `rust_prover_tools.kani_harness_cli` is the thin CLI adapter for
  `kani harnesses list` and `kani harnesses run`.
- `rust_prover_tools.supervision.launcher` owns the Cuprum boundary:
  `CuprumHarnessLauncher` starts one child in an owned process group and
  returns a `RunningHarness`. It alone imports Cuprum for launches, so the rest
  of the supervision layer stays prover-neutral and Cuprum-free.
- `rust_prover_tools.supervision.pipe_drain` borrows the child's pipe ends
  through `StdioTarget.fd`, forwards decoded lines, and translates the neutral
  `LaunchEnvironment` back to Cuprum's environment mode at that boundary.
- `rust_prover_tools.supervision.stream_lines` is the bounded splitter shared
  by the pipe drain and the streaming parser, so an unterminated line is capped
  in one place.
- `rust_prover_tools.supervision.cgroup` reads cgroup v2 membership and memory
  evidence from procfs and the mount table, returning explained absences rather
  than zeroes when the hierarchy is unavailable.

The adapter seam is deliberately small. A prover adapter supplies an observer
factory and a classifier:

```python
class HarnessAdapter(Protocol):
    def observer(self, spec: HarnessSpec) -> LineObserver: ...
    def classify(
        self, evidence: AttemptEvidence, observer: LineObserver
    ) -> HarnessClassification: ...
```

The observer accumulates facts while output streams; the classifier receives
the supervisor's termination evidence. Neither reimplements timing,
measurement, evidence, or accounting.

### Deterministic tests

The supervision suite uses a virtual clock, a virtual sleeper that advances it,
a scripted launcher, scripted resource and cgroup probes, and an in-memory
JSONL sink. No supervision test outside the real-process witness sleeps. The
launcher's scripted child emits its lines synchronously and then either settles
or waits to be stopped, so an immediate completion cannot race a monitor tick.

The real-process witness proves two properties doubles cannot: a stop reaches
the whole owned process group, and a silent child still produces heartbeats.
The live 0.67.0 fixture test is opt-in through `RPT_LIVE_KANI=1` and exercises
one passing proof, one `#[path]` proof, and one deliberately failing proof.

### Qualify-once rationale

A run must not observe one installation, generation, environment, or grammar
while launching another. `qualify_kani_installation` and
`prepare_snapshot_proof` therefore perform their validation, identity probes,
activation read, and environment construction once; every harness command is
derived from the result. A pointer switch after preparation cannot move the run
to another generation, and a concurrent snapshot repair cannot change the
launcher between harnesses.

## Kani snapshot producer recipe

`rust_prover_tools.kani_snapshot.producer` is the producer-side domain package.
It decides what may be built and how, and executes nothing: no subprocess,
network call, environment read or filesystem write belongs in it. Its sibling
`rust_prover_tools.kani_snapshot.orchestrator` carries every decision out — it
reads and writes the job documents, runs the pinned commands and performs the
bounded `gh` reads, and it is the only layer a workflow job invokes. The
dependency direction is one way: the orchestrator imports the producer, never
the reverse. `tests/test_kani_snapshot_producer_purity.py` reads the producer's
syntax trees and refuses an effectful import, an effectful call or an
orchestrator import, so the boundary cannot erode silently. The package is not
named `build`: the repository-wide `build/` ignore rule drops such a directory
from version control at any depth, and the code indexer skips it too.

- `recipe_model` holds the frozen records (`Recipe`, `UpstreamIdentity`,
  `BuilderRecord`, `LauncherRecipe`, `AttestationRecipe`, `LicenceRecord`).
- `recipe_rules.validate_recipe` is the gate both validated entry points use:
  `pinned_recipe` and `recipe_parse.parse_recipe`. The frozen records are plain
  dataclasses, so constructing one directly bypasses the gate. It checks every
  invariant, including that the compiler floor is reachable and no newer than
  the pinned compiler, and that the runtime baseline equals the builder glibc —
  the only baseline a build in that image links against. The recipe's `2.35` is
  the intended baseline rather than an observed one: the measured prototype ran
  on Rocky Linux 10 and linked against glibc `2.39`, and the production build in
  `ubuntu-22.04` is EP-M4's deliverable.
- `recipe_evidence` records the pinned commit and its gitlinks as plain data.
  Validation is the only gate a document passes, so it cannot read a fixture or
  a checkout; a recipe naming the pinned commit must instead record exactly
  these gitlinks. `pinned_recipe` builds its records from the same tuple, so
  the rule and the recipe share one source.
- `recipe` fixes the EP-M1 pin, renders canonical JSON and digests it;
  `recipe_parse.parse_recipe` reads that JSON back strictly.
- `upstream_metadata` parses the pinned upstream files the recipe must agree
  with: `rust-toolchain.toml`, `Cargo.toml`, `kani-dependencies`, `.gitmodules`
  and `license-notes.txt`.
- `resolution` selects an upstream commit from release and check-run responses,
  or validates a manually dispatched full SHA.
- `commands` derives argument vectors and asset names from validated inputs.
- `compiler_floor` owns the supported floor and the transcript checks that
  demonstrate it.

The documents and decisions handed between the guarded jobs have their own
modules, each owning one boundary rather than one file format:

- `lifecycle_model` holds the records a job writes and the next job strictly
  parses; `lifecycle` renders and parses that canonical JSON and derives the
  build plan, so one key set serves both directions. A resolution or plan is
  bound to the run and attempt that produced it, and a stale artefact cannot
  authorize a later effect.
- `publication_identity` allocates or reuses the revision,
  `publication_document` owns the identity document the resolve job hands to
  the build job, and `input_fingerprint` projects the declared build inputs
  into one canonical document so identity reuse is decided by the inputs rather
  than by upstream HEAD alone.
- `candidate_api` turns the GitHub release and check-run objects into validated
  shapes; `upstream_evidence` decides what a response may contribute to the
  evidence document, keeping fetching and shaping separate.
- `evidence_model` holds the build evidence the build job measured, and
  `assemble_input` the metadata it hands to assembly. Neither carries payload
  digests: assembly re-reads and hashes every payload itself, so a build job
  cannot smuggle a claim into the manifest.
- `asset_names` derives the release's asset names from validated records, so
  assembly, publication and the receipt share one ordering.
  `validation_receipt` records the clean validation that releases the publish
  job and refuses a receipt from another run, a different manifest digest or an
  incomplete asset set.

The publication boundary is split the same way. `release_model` holds the
port-facing records as frozen dataclasses, and `release_policy.decide` maps one
observation to the next action without performing any effect, so the decision
rules are testable without a release. Those rules include refusing a tag that
no release carries, which a new draft would adopt, and a published release
whose tag does not resolve; `ObservedRelease.tag_commit` is the observation
that carries the resolution, and the adapter reports it rather than acting on
it. `release_assets_policy.classify_assets` classifies the assets a draft
serves, `release_markers` reads and writes the release-body markers, and
`release_port` declares the `ReleaseApi` port. `publish` owns that port and is
the only caller of `publish_release`: it observes, decides, acts and observes
again, re-observes and re-decides before the single irreversible publish call,
and stops after a bounded number of steps (`max_steps`, 16 by default) instead
of assuming a state it cannot reconcile. `publish_steps.advance` is one step:
it decides from a reading, records the decision, and performs the effect that
decision names, including the pre-publish recheck and the write itself.
`publish_budget` decides what a spent budget may claim, from `StepOutcome`:
whether the irreversible call was entered at all, and the newest reading the
run holds. A budget that ends on the publish call takes one confirming
observation and reports an unconfirmed publication — or the conflict that
observation found, with its reason — and never a state that did not converge. A
budget that ends on the recheck which withdrew authority for the write decides
on the reading that withdrew it: a release the recheck found already published
and verified is reported as the no-op it is, an observed conflict as the policy
conflict it is, with its reason, and any other withdrawal, such as an asset the
recheck found missing, as a state that did not converge — unless an earlier
step entered the publish call, in which case an unconfirmed publication is
reported instead. The same warning covers a failure after the call was entered:
the publish call's own failure, or a read or verification the run cannot
complete, each keeps its original error as the cause, because the retry such an
error invites would be a second publication. The warning is stated once, because
`PublicationUncertainError` marks an error that already carries it.
`publish_observation` decides when two readings describe the same release,
which is what lets a completed download be believed only if the release did not
move while it ran. `publish_request` holds the record of what one attempt is
asked to publish; checking the local files against the manifest reads and
hashes them, so `publish` owns that precondition and runs it before any release
API call. The policy these steps carry is D5 in the distribution design
(`docs/kani-snapshot-distribution-design.md#d5-workflow-and-immutable-publication`);
this guide names the code rather than restating the rules.

`gh_transport` is the one place a `gh` failure is classified. It owns the
`GhRunner` protocol the adapter runs commands through, the read budget (120 s)
and the transfer budget (300 s), and it treats a timeout, exit code 124 or a
transient stderr marker as retryable while the permanent authentication and
permission markers take precedence. `gh_optional` returns `None` only for a
confirmed HTTP 404, so "not found" and "could not ask" never collapse into one
answer, and `GhReleaseApi` refuses any repository but `REPOSITORY`. On the
consumer side, `install_sender` performs the release transport behind
`kani snapshot install`, and `shared_libraries` owns the dynamic-library search
the installed tools use.

The orchestrator's own helpers are named in the workflow section below:
`documents` writes job documents atomically, `inventory_build` walks a payload
tree and `inventory_carriers` decides from the archive headers which path
carries a hard link's bytes. `operations` holds the effect of each producer job
as one callable, leaving `entrypoint` a dispatch table; `resolve_operations`
owns the resolve job and the workflow identity it first binds to a document; and
`release_evidence` derives the complete asset set one publication must serve,
including the checksums and attestation the manifest does not name.

Three helpers on those paths carry their own contract.
`documents.write_document` writes and renames a job document so a reader never
sees a partial one. `inventory_parse.normalize_inventory_path` applies the one
path grammar both the producer and the consumer use, removing empty and `.`
components and refusing a name either side could not record, which is what
makes a comparison between a tarball member and an extracted tree meaningful.
`producer.commands.launcher_extract_command` derives the argument vector that
unpacks a packaged launcher crate beside its crate file, so the producer
validates and the consumer verifies the same spelling.

The package boundary is defined by effects, not by the shape of the data it
reads. `resolution` converts GitHub API payloads, `upstream_metadata` converts
upstream files and `commands` derives argument vectors; all three are pure
functions with no outbound port, so moving them behind an "adapter" would not
change a single dependency direction. Keeping them here keeps each fail-closed
check next to the records it protects and keeps the whole package testable
without a service. The `attestation` verifier and its frozen `command_prefix`
are recipe facts for the same reason: the producer validates the prefix a
recipe records, `attestation_command` builds the consumer's argument vector
from its own fixed `gh attestation verify` spelling, and
`test_attestation_command_prefix_matches_the_recipe` fails if the two drift
apart. Nothing compares the prefix at run time, so moving the recipe side into
an adapter would leave that agreement unenforced.

Conventions that keep the recipe trustworthy:

- Argument vectors are tuples, never shell strings. Every value that reaches
  one is validated first: plain semver, full lowercase SHAs, contained relative
  paths and safe basenames.
- `recipe_to_json` is byte-stable (sorted keys, two-space indent, ASCII, final
  newline). `recipe_digest` hashes those bytes, and later milestones record the
  digest in the manifest, so any pinned-value change changes the identity. A
  Syrupy snapshot of the canonical document records what changed; refresh it
  with `pytest --snapshot-update` only when the pin change is intended.
- The pinned upstream identity is the commit the release tag points at, never
  `main`'s head. When re-pinning, refresh `tests/fixtures/kani_recipe` with the
  upstream files from that same commit; the unit tests compare the recipe
  against them.
- The non-version fixtures are evidence, not inputs. `upstream-release.json`,
  `upstream-tag-ref.json` and `upstream-check-runs.json` are the release, tag
  ref and check-run responses for the pinned commit, deduplicated. Their shape
  is the API's, not a normalized copy: a release's `target_commitish` is a
  branch name (`main` for Kani), so the released commit is read from
  `refs/tags/<tag_name>`, and an annotated tag must be dereferenced to its
  commit before it is recorded. `upstream-gitlinks.json` records the recursive
  submodule commits.
- The compiler floor is demonstrated by two transcripts under
  `tests/fixtures/kani_snapshot/compiler_floor`. `positive.txt` is a real Kani
  verification of the named harness, and `negative_1_93.txt` is the control
  compiler (Rust 1.93.1) rejecting `array_windows` with `E0658`. Recapture both
  if the floor changes; never hand-edit them. Recapture from the fixture
  directory with:

  ```bash
  # Positive: the installed snapshot verifies the harness. Terse output keeps
  # the transcript to the header, the harness line and the verdict.
  kani positive.rs --output-format terse > positive.txt 2>&1
  # Negative: the control compiler refuses the same source.
  rustc +1.93.1 negative_1_93.rs 2> negative_1_93.txt
  ```

- The builder image is part of the recipe. Dynamically linked Rust binaries
  cannot claim a glibc floor older than the image that linked them, and
  `kani-driver` links the host C library, so a prototype built on another
  distribution measures that distribution's floor, not the published one.

## Kani snapshot producer entrypoint

`python -m rust_prover_tools.kani_snapshot.orchestrator.entrypoint` is the
developer interface `.github/workflows/kani-monthly.yml` calls. It is not a
consumer API: the CLI does not register these subcommands, and nothing outside
the snapshot workflow should depend on them. Each subcommand reads the
documents earlier jobs produced, checks their identity and same-run binding,
and only then performs its effect, so a stale or foreign document fails before
anything is written.

| Subcommand | Inputs                                                                                                    | Effect                                             |
| ---------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `resolve`  | `--route scheduled --candidates` or `--route manual --commit`, plus `--output` and the run identity flags | Writes the resolved upstream commit.               |
| `identity` | `--resolution`, `--builder-commit`, and either `--month --releases` or `--publication-id`                 | Writes the immutable publication identity.         |
| `build`    | `--resolution`, `--identity`, `--builder-commit`                                                          | Writes the pinned build plan.                      |
| `assemble` | `--input` assembly input, `--identity` the resolve job's `identity.json`, `--payloads` and `--output`     | Writes `manifest.json` and `SHA256SUMS`.           |
| `validate` | `--manifest`, `--payloads`                                                                                | Writes the receipt naming the checks it performed. |
| `publish`  | `--manifest`, `--receipt`, `--payloads`                                                                   | Drives the release to a published, verified state. |

Every subcommand also takes `--run-id`, `--run-attempt`, `--event` and
`--run-url`. Those four flags are the workflow identity the receipt and
publication refuse to confuse: a document from another run or attempt is
rejected even when its bytes are otherwise identical.

The workflow's shell is allowed to compose observed documents and to execute a
plan, but not to interpret one. Anything that projects an external response
into a document the next job consumes belongs in a tested module. The
`orchestrator` collectors (`collect_candidates`, `collect_releases`) and the
file steps (`measure_build`, `build_evidence`, `inventory_build`, `assembly`)
each own one such projection; the decisions they apply live in the producer
modules those steps call.

Fetching and shaping are separate modules as well.
`orchestrator.collect_candidates` performs the upstream `gh` reads;
`producer.upstream_evidence` decides what a response may contribute and renders
the document deterministically. Both collectors take a `GhRunner`, so a test
asserts the exact argument vector each job sends without starting a process,
and a transport failure keeps its transient or permanent classification through
the added context.

Publication follows the same rule from the other side: `publish` depends only
on the `ReleaseApi` port in `release_port`, `gh_release.api` is its one
adapter, and `orchestrator.entrypoint` is the composition root that chooses it.
`observe` is a query that downloads nothing; `hash_assets` downloads only the
assets a draft edit depends on and reports the digests of their bytes; `verify`
downloads, hashes and authenticates the served bytes as a whole. The policy
asks for each explicitly, so a digest listed by the API is a claim until a hash
or a verification has computed the same value locally. A draft that needs an
edit — a placeholder to delete or a missing asset to upload — is hashed first,
so a same-size asset whose bytes differ is refused before the draft is changed;
a draft that needs no edit is verified whole instead, and that download hashes
it. The write that would publish the draft is then decided from a reading taken
after that download: the first listing cannot show an asset removed while its
bytes were being hashed, nor a draft retargeted or re-bodied mid-download, so
publication reads the draft again, restores anything that disappeared and
refuses an identity the policy no longer approves before anything becomes
public. The confirming read follows every download whose verdict authorizes a
later step — the byte check before an edit and the verification behind a
published no-op as well as the draft's own verification — and a verdict reached
across a change is discarded rather than reused. The gap between that read and
the write remains, because GitHub's release update has no conditional form; the
observation after the write reports a divergence there. Each side of that
boundary has one module: `release_assets_policy` classifies an observed asset
set, `release_markers` reads and writes the release-body markers, and the
adapter's steps are `gh_release.document` for the served documents,
`gh_release.attestation` for signature policy and `gh_release.download` for the
download, digest and verification sequence. A change to one seam therefore
cannot silently reach across into another.

Scheduled runs collect the existing release list inside the read-only `resolve`
job, because identity allocation is a decision about the tags that already
exist. GitHub lists draft releases only to a push-capable token, so that
document holds the published releases alone; a draft left by an interrupted run
stays invisible to allocation and is refused by publication, which observes the
tag again with the writer token and will not adopt a draft bound to another run
or attempt. A manual dispatch names its own publication id and collects nothing.

`validate` records the checks `verify_release` performs, so a caller cannot
label work that never ran; the `attestation` check is a presence and non-empty
check of `manifest.sigstore.jsonl`. The signature policy —
`gh attestation verify` with explicit repository, workflow, ref, source and
signer digests, correlated with the manifest's run and attempt — is the
consumer's, and EP-M5 exercises it on a clean runner. Do not describe the
receipt as proof that a signature was verified.

The publisher's own release observation does run that policy. When a published
release is re-verified from its downloaded bytes, the structural attestation
checks are followed by `verify_attestation` with the consumer's
`AttestationPolicy`, so a bundle signed by another workflow, or for another run
or attempt, leaves the release unverified instead of reaching a no-op. The
ledger still rests on EP-M5: a real clean-runner signature has not yet been
exercised end to end.

The producer and the consumer share `shared_libraries`:
`orchestrator.measure_build` hashes every shared object under `<toolchain>/lib`
that the function returns, and `toolchain_binding` refuses any it returns that
the manifest does not record. Do not reintroduce a workflow-supplied list — a
narrow list would let a build publish a toolchain the consumer then refuses.

Identity reuse is decided by `input_fingerprint`, a SHA-256 over a canonical
document of declared inputs: the upstream commit, this repository's build
commit, and the pinned recipe's toolchain, prerequisites, submodules, runtime
and builder facts. Run and attempt numbers, timings, cache outcomes and asset
sizes are deliberately excluded, because a rebuild of unchanged inputs produces
a new manifest but the same inputs. The `identity` subcommand writes that
fingerprint into `identity.json`, `publish` recomputes it from the manifest's
commits, and `release_body` records it in the release body next to the creating
run and attempt. A draft is resumable only by that run and attempt; a published
release answers with a no-op only when the fingerprint matches and the adapter
re-verified the release's own assets against its own manifest. Nothing compares
this run's manifest digest with the published one, because they legitimately
differ.

### Workflow contract tests

Workflows are executable policy, so they are tested like code.
`tests/workflow_contracts/strict_workflow.py` reads them the way GitHub reads
them: duplicate keys are refused rather than silently keeping the last one, and
`on:` stays a string instead of becoming YAML 1.1's boolean. The helpers return
steps, scripts, `needs` and artefact names without assuming a scalar or a list,
because both are valid YAML for the same field.

`tests/test_kani_workflow_contract.py` pins the snapshot workflow's guards,
per-job permissions, Action pin shape, artefact routing, job budgets, the order
of attestation, validation and publication, and the recipe facts the workflow's
declared environment must match. `tests/test_python_release_contract.py` pins
the draft-first Python release lifecycle, including that a rerun of an
already-published release is a verified no-op rather than a repair. Both follow
the pin policy above: they assert that a reference is a full 40-hex commit,
never that it equals a particular one.

Run them together with actionlint:

```sh
make workflow-check
```

actionlint is a required host tool for `make actionlint`, `make workflow-check`
and `make all`. The repository pins 1.7.12: CI downloads the Linux AMD64
release archive for that version and verifies it against a pinned SHA-256
checksum before installing it (`.github/workflows/ci.yml`, the "Install
actionlint" step). A developer host must provide an `actionlint` of the same
version on `PATH`, either from its package manager or by the same
download-and-verify route. `ACTIONLINT` overrides the binary the Makefile
invokes, for a host that installs it elsewhere, and `ensure_tool` fails with
`'<path>' is required, but not installed` when the resolved binary is absent.
actionlint lints `.github/workflows/*.yml` only; it cannot lint a composite
action, so `tests/test_python_release_contract.py` reads
`.github/actions/pure-python-wheel/action.yml` directly.

### Documented examples

A user-facing example carries a `<!-- tested-example: <identifier> -->` marker
directly above its fence. `tests/documentation_examples.py` is the shared
loader: it reads the published document and refuses a marker with no fence, an
unterminated fence, or a duplicate identifier.
`tests/test_documented_examples.py` registers the users' guide's smoke
dispatch. That example cannot be executed in a test because it submits a hosted
workflow run, so the test holds the boundary it does share with the code: the
documented command and the Makefile recipe must name the same target and inputs.

The workflow gates also need `jq` on `PATH`. The build job's run summary is a
`jq` filter, and `tests/test_kani_workflow_build_contract.py` runs that filter
over a real evidence document rather than trusting its text, so both
`make test` and `make workflow-check` fail loudly when `jq` is missing. It is a
host tool the repository does not pin: the GitHub `ubuntu-latest` runner ships
it, and a developer host needs it installed.
`tests/test_python_release_lifecycle.py` additionally runs the wheel workflow's
own steps against a stand-in `gh`, which uses only `bash` and the Python
standard library.

`make kani-snapshot-smoke` dispatches a real, non-publishing snapshot run. It
is opt-in and never part of `all`, because it builds on a hosted runner and
creates an attestation.
