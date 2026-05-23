# Convert prover shell scripts into one Cyclopts CLI

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
 `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`,
and `Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

## Purpose / big picture

This change introduces one Python command-line interface (CLI) for installing,
checking, and running Rust prover tooling. It replaces four project-specific
shell scripts from the Netsuke and Chutoro repositories with one noun/verb
interface built with Cyclopts:

```plaintext
prover-tools kani install
prover-tools kani check-version
prover-tools verus install
prover-tools verus run
```

After implementation, users can run the same externally observable workflows as
the original shell scripts, but through a tested Python package that follows
this repository's scripting standards. Success is visible when the new CLI can
read pinned Kani and Verus versions, invoke the expected external commands
through Cuprum, validate command outputs and checksums, and pass the full local
gate sequence:

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-rust-prover-tools-initial-tool-import.out
make typecheck 2>&1 | tee /tmp/typecheck-rust-prover-tools-initial-tool-import.out
make lint 2>&1 | tee /tmp/lint-rust-prover-tools-initial-tool-import.out
make test 2>&1 | tee /tmp/test-rust-prover-tools-initial-tool-import.out
```

The user approved implementation on 2026-05-22. Keep this plan current as
milestones complete, and run `coderabbit review --agent` after each major
milestone before moving on.

## Constraints

The branch is `initial-tool-import`, so this plan lives at
`docs/execplans/initial-tool-import.md`.

The repository-level `AGENTS.md` requires reading local guidance, keeping plans
current, using Makefile targets over ad-hoc commands, running long validation
commands through `tee`, not running format, lint, typecheck, or test commands
in parallel, and committing only changes that pass the relevant gates.

The requested `$leta` skill was loaded and the workspace was added with:

```bash
leta workspace add /data/leynos/Projects/rust-prover-tools
```

The requested `$kani` and `$verus` skill files were not present at
`/home/leynos/.codex/skills/kani/SKILL.md` or
`/home/leynos/.codex/skills/verus/SKILL.md` in this session. Implementation
must therefore use repository evidence, upstream script behaviour, and current
public documentation rather than unavailable local skills.

Use GrepAI as the primary semantic search tool for intent-based code
exploration. Use Leta for symbol-aware navigation and refactoring after source
symbols exist. Use exact text search only for literal strings, docs, config, or
file patterns.

Use Cyclopts for the public CLI. The CLI must follow an environment-first
configuration model using the `INPUT_` prefix and preserve legacy environment
variables where the shell scripts already define them.

Use Cuprum for external command invocation. Do not introduce direct
`subprocess`, shell-string execution, or Plumbum-based command execution in the
new feature, even though `docs/scripting-standards.md` still contains older
Plumbum examples.

Use CmdMox for mocking external commands in unit tests. Tests must not require
real `cargo`, `rustup`, `curl`, `unzip`, `sha256sum`, `shasum`, or `verus`
executions except in explicitly marked end-to-end tests.

Add unit tests using `pytest`, behavioural tests using `pytest-bdd`, snapshot
tests using `syrupy` where output format consistency matters, and property
tests using Hypothesis or a bounded model checker such as CrossHair when pure
helpers introduce invariants over a range of inputs.

End-to-end tests must be added for externally observable workflows,
command-line behaviour, network boundaries, and installer/runner integration
contracts. They must be deterministic by default and must not download real
release artefacts in normal `make test` runs.

Update user-facing behaviour in `docs/users-guide.md`. Record design decisions
in a design document or, if substantive, an architectural decision record
(ADR). Document internally facing interfaces and conventions in
`docs/developers-guide.md`; create that document if it does not exist.

All documentation must follow `docs/documentation-style-guide.md`, including
British English with Oxford spelling, sentence-case headings, 80-column prose
wrapping, Markdown code fence languages, and ordered list numbering.

## Tolerances

Escalate before implementation continues if any command needs to write build
outputs to `/tmp`; `/tmp` may be used only for logs or scratch files.

Escalate if the new CLI cannot preserve an existing script's success or failure
semantics without breaking a clear Python packaging or safety constraint.

Escalate if a required dependency is unavailable from the configured package
indexes or cannot satisfy the current `requires-python = ">=3.14"` setting.

Escalate if adding CrossHair to the default `make test` path makes the test
gate non-deterministic or materially slow. In that case, keep Hypothesis in the
default gate and propose an opt-in CrossHair target.

Escalate if implementing the Verus installer requires unbounded platform
support beyond the current script contract. The existing default target is
`x86-linux`; support for additional targets should be data-driven but does not
need to invent a full platform resolver in the first milestone.

Escalate if full validation cannot complete within the command timeout of 1200
seconds per command. Split the command or report the exact failing or timed-out
gate with the log path.

## Risks

The original shell scripts rely on ambient shell behaviours. The script
`check-kani-version.sh` splits `KANI` with shell word splitting, while Python
should use `shlex.split` to preserve quoted command arguments without invoking
a shell. Tests must cover quoted commands and empty commands.

The Verus installer downloads a release zip from GitHub, validates a SHA-256
checksum, unzips into an install directory, and normalizes the extracted
directory to `verus`. Unit and behavioural tests must separate URL
construction, checksum lookup, checksum verification, archive extraction, and
directory normalization so the logic can be validated without network access.

`run-verus.sh` calls `install-verus.sh` as a fallback when the binary is
missing. The Python CLI should call an internal installer function instead of
recursing through a second process, but the externally visible behaviour must
remain the same: if the default Verus binary is missing, installation is
attempted before failing.

Cuprum enforces a command catalogue and allowlist model. The implementation
must define a project-specific catalogue for exactly the external programs
needed by the prover workflows. Missing catalogue entries will surface as
Cuprum errors rather than command-not-found errors unless the implementation
maps them clearly.

CmdMox intercepts executables through generated shims on `PATH`. Cuprum's
allowlist and command builders must remain compatible with that interception so
tests can mock command invocations without bypassing the production execution
path.

The current repository has minimal package code and no existing design or
developer guide. Creating those documents increases the documentation scope,
but is required because this feature introduces internal command-building and
test conventions.

## Progress

- [x] 2026-05-22: Read repository `AGENTS.md` and confirmed branch
  `initial-tool-import`.
- [x] 2026-05-22: Loaded the `$leta` skill instructions and created the Leta
  workspace.
- [x] 2026-05-22: Checked for requested `$kani` and `$verus` skills and found
  no local skill files in this session.
- [x] 2026-05-22: Used a Wyvern sub-agent for read-only planning assistance.
- [x] 2026-05-22: Used Firecrawl for structured extraction from the four source
  scripts and the Cuprum and CmdMox guides.
- [x] 2026-05-22: Read the four upstream shell scripts directly from their raw
  GitHub URLs and extracted their contracts.
- [x] 2026-05-22: Read local scripting, documentation, user-guide, Makefile,
  and Python rules context.
- [x] 2026-05-22: Drafted this plan.
- [x] 2026-05-22: User approved implementation of this plan.
- [x] 2026-05-22: Added runtime and test dependencies for the CLI
  implementation.
- [x] 2026-05-22: Implemented tests covering pure helpers, Kani and Verus
  orchestration, behavioural CLI scenarios, end-to-end Verus CLI execution,
  snapshots, and Hypothesis properties.
- [x] 2026-05-22: Implemented the CLI and internal command modules.
- [x] 2026-05-22: Ran a CodeRabbit review for the first implementation
  milestone and addressed its actionable concerns around docstrings, stable
  assertions, command help text, and over-broad lint suppressions.
- [x] 2026-05-22: Split Verus implementation into package modules and added
  behavioural scenarios for successful proof execution and missing Verus
  toolchain reporting.
- [x] 2026-05-22: Re-ran focused behavioural, snapshot, and Verus unit tests
  after the CodeRabbit fixes:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    tests/steps/test_prover_tools_cli_steps.py \
    tests/test_prover_tools_snapshots.py \
    rust_prover_tools/unittests/test_verus.py
  ```

- [x] 2026-05-22: Ran a second CodeRabbit review for the revised
  implementation milestone. It raised 12 actionable findings covering
  timeout-bounded downloads, Verus module docstrings, CLI lint-suppression
  rationale, structural pattern matching for error exits, idiomatic Syrupy
  assertions, E2E proof-output checks, deterministic executable permissions,
  and additional Kani unit coverage.
- [x] 2026-05-22: Addressed the second CodeRabbit review findings and re-ran
  the affected tests:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_kani.py \
    rust_prover_tools/unittests/test_verus.py \
    tests/test_prover_tools_cli_e2e.py \
    tests/test_prover_tools_snapshots.py \
    tests/steps/test_prover_tools_cli_steps.py
  ```

- [x] 2026-05-22: Ran a follow-up CodeRabbit review. It requested explicit
  stdout handling for Verus wrapper output, richer module and public-function
  docstrings, and clearer assertion messages in command tests.
- [x] 2026-05-22: Addressed the follow-up review findings and re-ran focused
  tests:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_commands.py \
    rust_prover_tools/unittests/test_kani.py \
    rust_prover_tools/unittests/test_verus.py \
    tests/test_prover_tools_cli_e2e.py \
    tests/test_prover_tools_snapshots.py \
    tests/steps/test_prover_tools_cli_steps.py
  ```

- [x] 2026-05-22: Ran another CodeRabbit review and addressed its remaining
  four findings: explicit snapshot assertion diagnostics, Kani override source
  reporting, and a corrected `main()` docstring. Re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_kani.py \
    tests/test_prover_tools_snapshots.py \
    tests/steps/test_prover_tools_cli_steps.py
  ```

- [x] 2026-05-22: Ran a further CodeRabbit review and addressed its 21
  findings: en-GB wording, expanded public Verus API docstrings, example proof
  path naming, reduced duplicate default-binary resolution, archive
  verification assertions, supported Verus binary layouts, and runner branch
  coverage for toolchain install, proof failure, install fallback, invalid
  override fallback, and missing proof files. Re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_verus.py
  ```

- [x] 2026-05-22: Ran another CodeRabbit review and addressed its final two
  findings: consistent `_print_lines` usage in `check_version`, and
  `read_required_file` failure documentation. Re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_versions.py \
    tests/steps/test_prover_tools_cli_steps.py
  ```

- [x] 2026-05-22: CodeRabbit was temporarily rate-limited during the next
  clearance attempt. After cooldown, another review completed with six findings
  around Cuprum metadata rationale, list-based Kani message building,
  parameterised Verus layout tests, `_print_lines` documentation, POSIX shell
  fake Verus scripts, and BDD fixture deduplication. Addressed them and re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_commands.py \
    rust_prover_tools/unittests/test_kani.py \
    rust_prover_tools/unittests/test_verus.py \
    tests/steps/test_prover_tools_cli_steps.py
  ```

- [x] 2026-05-22: Ran another CodeRabbit review and addressed its single
  remaining en-GB spelling finding in `CommandFailedError`. Re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_commands.py
  ```

- [x] 2026-05-22: Attempted another CodeRabbit clearance review. The service
  returned a recoverable rate-limit error before analysis with a 4 minute 20
  second wait. Proceeding to the documentation milestone because the last
  concrete CodeRabbit finding has been fixed and locally validated; retry
  CodeRabbit after documentation updates.
- [x] 2026-05-22: Updated `docs/users-guide.md` with `prover-tools`
  commands, options, environment variables, output behaviour, and compatibility
  notes.
- [x] 2026-05-22: Added `docs/prover-tools-cli-design.md` to record the CLI
  architecture, command invocation design, test strategy, decisions, and
  operational notes.
- [x] 2026-05-22: Added `docs/developers-guide.md` to document internal
  command invocation, cmd-mox testing, and documentation maintenance
  conventions.
- [x] 2026-05-22: Ran CodeRabbit for the documentation milestone after a short
  rate-limit cooldown. Addressed eight findings covering Kani success-message
  source reporting, redundant stdout stripping, en-GB spelling in docs and test
  names, the configurable example Verus proof path, and
  `VerusProofFailedError.exit_code` documentation. Re-ran focused tests and
  `make fmt`.
- [x] 2026-05-22: Ran a final CodeRabbit clearance attempt and addressed four
  findings: Oxford `normalization`, Kani source-string reuse, expanded
  `errors.py` and Kani test module docstrings. Re-ran:

  ```bash
  UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
    rust_prover_tools/unittests/test_kani.py \
    rust_prover_tools/unittests/test_commands.py \
    tests/steps/test_prover_tools_cli_steps.py
  make fmt 2>&1 | tee /tmp/fmt-rust-prover-tools-initial-tool-import.out
  ```

- [x] 2026-05-22: Ran another CodeRabbit clearance attempt and addressed three
  findings: structured `KaniPaths.resolved_version_file` documentation, en-GB
  spelling in `errors.py`, and shared fake Verus binary creation through a
  pytest fixture. Re-ran focused Kani, command, BDD, and E2E tests plus
  `make fmt`.
- [x] 2026-05-22: Ran another CodeRabbit clearance attempt and addressed five
  findings: developer guide imperative wording, Cyclopts app annotations, and
  explicit `__all__` exports for `commands`, `errors`, and `versions`. Re-ran
  focused commands, versions, and snapshot tests plus `make fmt`.

- [x] 2026-05-22: Stopped further CodeRabbit nit-chasing at user direction
  after the latest concrete findings were fixed. Any remaining polish can be
  handled in code review.
- [x] 2026-05-22: Ran and passed the required final gates:

  ```bash
  make check-fmt 2>&1 | tee /tmp/check-fmt-rust-prover-tools-initial-tool-import.out
  make typecheck 2>&1 | tee /tmp/typecheck-rust-prover-tools-initial-tool-import.out
  make lint 2>&1 | tee /tmp/lint-rust-prover-tools-initial-tool-import.out
  make test 2>&1 | tee /tmp/test-rust-prover-tools-initial-tool-import.out
  make markdownlint 2>&1 | tee /tmp/markdownlint-rust-prover-tools-initial-tool-import.out
  make nixie 2>&1 | tee /tmp/nixie-rust-prover-tools-initial-tool-import.out
  ```

- [x] 2026-05-22: Committed the approved, gated change as
  `77100fe Add prover tools CLI`.
- [x] 2026-05-23: Addressed review feedback that `verus run` did not forward
  the selected Verus release target into auto-install fallback. Added
  `VerusRunOptions.target`, exposed `--target` on `prover-tools verus run`,
  forwarded it to `VerusInstallOptions`, and covered a non-default target in
  `test_run_verus_forwards_target_to_missing_binary_installer`.

## Surprises & discoveries

The repository currently has only a stub package: `rust_prover_tools/pure.py`
defines `hello`, and there is no existing CLI, design document, or developer
guide.

`pyproject.toml` targets Python 3.14, while `docs/scripting-standards.md` says
new scripts target Python 3.13. Because the package already declares Python
3.14, implementation should use Python 3.14 syntax only where it improves
clarity and should not lower the project runtime without an explicit decision.

`docs/scripting-standards.md` still recommends Plumbum for external processes,
but the user explicitly requested Cuprum for this feature. The plan treats
Cuprum as the feature-specific override.

The Verus scripts use `tools/verus/VERSION` and `tools/verus/SHA256SUMS`, while
the Kani scripts use `tools/kani/VERSION`. The first implementation should keep
these defaults configurable because this repository may not itself own those
tool pin files yet.

Firecrawl's structured extraction corroborated the source-script contracts in
this plan. Treat the upstream scripts and the Cuprum and CmdMox guides as the
source of truth during implementation if any generated summary conflicts with
the primary material.

Running `leta files | head -n 240` after earlier validation walked generated
cache content and the Leta process aborted on a broken pipe when `head` exited.
Use targeted Leta commands and avoid piping full workspace output through
`head` while generated cache directories are present.

CodeRabbit flagged the original single-file Verus implementation as too large
after the first milestone. The implementation now keeps the public
`rust_prover_tools.verus` import surface but moves installation helpers, run
helpers, and shared models into `rust_prover_tools/verus/install.py`,
`rust_prover_tools/verus/run.py`, and `rust_prover_tools/verus/models.py`.

The Verus runner must stream the verifier's own output to standard output. The
original shell script let the verifier write directly to the terminal, and the
behavioural test now checks that a successful proof result remains visible to
callers of `prover-tools verus run`. Any Python wrapper lines returned by
`run_verus` are also printed to standard output so the command stream is
consistent with the other CLI verbs.

CodeRabbit identified the unbounded `curl` download as a reliability gap. The
installer now calls `curl -sSfL --connect-timeout 15 --max-time 300 ...` so a
stalled download fails within a bounded interval while preserving the original
URL and output-file contract.

Kani status and mismatch messages now distinguish version-file sources from
explicit version overrides. This avoids misleading users when `--version` or
`--expected-version` is used.

The default proof path is now named `EXAMPLE_VERUS_PROOF_PATH` in code to make
clear that it is a compatibility default derived from the source script, not a
universal project convention. Callers should pass `--proof-file` or
`VerusRunOptions.proof_file` for project-specific proof entry points.

Review feedback identified that Verus auto-install fallback must preserve the
selected release target. Without this, `verus run --target <target>` can
download the default `x86-linux` release during fallback, even when the caller
needs a different archive.

## Decision Log

2026-05-22: Use a package console script rather than a standalone `scripts/`
file. The current project is a Python package with `pyproject.toml`, Makefile
gates, and strict type checking. A console script such as
`prover-tools = "rust_prover_tools.cli:main"` keeps imports, tests, and type
checking under the package rather than treating the CLI as an isolated script.

2026-05-22: Use a noun/verb command graph with `kani` and `verus` as nouns and
`install`, `check-version`, and `run` as verbs. This matches the user's request
and maps directly onto the four source scripts without inventing unrelated
commands.

2026-05-22: Preserve legacy environment variables while adding `INPUT_`
configuration. The old scripts expose `KANI`, `VERUS_TARGET`,
`VERUS_INSTALL_DIR`, `VERUS_BIN`, and `VERUS_PROOF_FILE`. Existing automation
may depend on those names, while local scripting standards favour `INPUT_`
variables.

2026-05-22: Model external commands through small builder functions and a
project-specific Cuprum catalogue. This centralizes allowlisted programs such as
 `cargo`, `rustup`, `curl`, `unzip`, `sha256sum`, and `shasum`, keeps command
assembly testable, and avoids accidental shell execution.

2026-05-22: Keep real network downloads out of the default test suite. The
Verus installer must be testable with local fixture archives and CmdMox
responses. Any true network end-to-end test should be opt-in and clearly marked
because normal `make test` must remain deterministic.

2026-05-22: Run `coderabbit review --agent` after each major milestone and
resolve all actionable concerns before continuing. Treat a completed CodeRabbit
run with no actionable findings as milestone clearance.

2026-05-22: Use Hypothesis, not CrossHair, for the default invariant tests in
this milestone. The plan allows either Hypothesis or a bounded model checker;
Hypothesis has direct pytest integration and keeps the default `make test` gate
deterministic for the pure parsing helpers.

2026-05-22: Commit `uv.lock` with this implementation. The project previously
had no lockfile, but adding runtime and test dependencies makes the lock useful
for deterministic local validation with `uv sync --group dev`.

2026-05-22: Preserve `rust_prover_tools.verus` as a public import facade while
implementing Verus internals as a package. This resolves the large-module
review concern without forcing callers or CLI code to change imports.

2026-05-22: Run Verus proof commands with command echoing enabled through
Cuprum so proof output is streamed like the original shell workflow. Python
wrapper lines returned by the run helper are printed to standard output for
consistency with the other CLI commands.

2026-05-22: Bound Verus archive downloads with `curl --connect-timeout 15` and
`--max-time 300`. This preserves the shell script's `curl -sSfL` semantics but
prevents normal CLI workflows and tests from hanging forever on a stalled
network connection.

2026-05-22: Rename the hardcoded Verus proof default to
`EXAMPLE_VERUS_PROOF_PATH` and keep `VerusRunOptions.proof_file` as the
preferred explicit proof entry point. This makes the source-script
compatibility default visible without implying every caller should use that
path.

2026-05-23: Add `target` to `VerusRunOptions` and pass it through
`resolve_default_verus` to `VerusInstallOptions`. This keeps explicit Verus run
target selection consistent with installer fallback.

## Current source-script contracts

The original `install-kani.sh` script resolves the repository root from the
script location, reads `tools/kani/VERSION`, trims whitespace, validates that
the value uses `MAJOR.MINOR.PATCH`, requires `cargo` on `PATH`, runs:

```bash
cargo install --locked kani-verifier --version "$kani_version"
cargo kani setup
cargo kani --version
```

It prints status lines before installation, before setup, and before version
verification. Failures use an `install-kani:` prefix.

The original `check-kani-version.sh` script reads the same version pin and uses
`KANI` as a command override, defaulting to `cargo kani`. It appends
`--version`, parses the first `MAJOR.MINOR.PATCH` version from command output,
compares it to the pin, and prints:

```plaintext
Kani <version> matches <version_file>.
```

The original `install-verus.sh` script reads `tools/verus/VERSION` and
`tools/verus/SHA256SUMS`, uses `VERUS_TARGET` with default `x86-linux`, and uses
 `VERUS_INSTALL_DIR` with default `.verus/<version>` under the repository root.
It builds the archive name:

```plaintext
verus-<version>-<target>.zip
```

It downloads from:

```plaintext
https://github.com/verus-lang/verus/releases/download/release/<version>/<archive>
```

It checks the expected SHA-256 from the checksum file, calculates the actual
hash with `sha256sum` or `shasum -a 256`, unzips into the install directory,
renames the extracted `verus-*` directory to `verus`, and prints:

```plaintext
Installed Verus <version> in <install_dir>/verus
Export VERUS_BIN=<install_dir>/verus/verus
```

If `<install_dir>/verus/verus` already exists and is executable, it prints that
Verus is already installed and exits successfully.

The original `run-verus.sh` script reads `tools/verus/VERSION`, derives the
default Verus binary from `VERUS_INSTALL_DIR` and the version pin, and accepts
`VERUS_BIN` and `VERUS_PROOF_FILE` overrides. It resolves Verus from an
executable file, from known subpaths under a directory, or from a command on
`PATH`. If a non-default `VERUS_BIN` is invalid, it warns and falls back to the
default. If the default binary is missing, it runs the installer. It runs
`verus --version`, parses the required Rust toolchain from either
`Toolchain: <name>` or `required rust toolchain <name>` output, installs the
toolchain with `rustup toolchain install <name>` if needed, and runs Verus
against the proof file. On verifier failure, it prints the binary, proof file,
and toolchain, then exits with the verifier's status.

## Proposed CLI surface

Expose one console script named `prover-tools` from
`rust_prover_tools.cli:main`.

The top-level module should define a Cyclopts app with `INPUT_` environment
configuration. A small command tree should register Kani and Verus sub-apps.
All command functions should be thin orchestration wrappers around typed helper
functions in feature modules.

`prover-tools kani install` should support:

- `--repo-root PATH`, defaulting to the current working directory unless a
  clearer package rule is introduced during implementation.
- `--version-file PATH`, defaulting to `tools/kani/VERSION` under
  `repo-root`.
- `--version TEXT`, allowing callers and tests to bypass the version file.
- `--setup / --no-setup`, defaulting to setup enabled.
- `--verify / --no-verify`, defaulting to verification enabled.

It should also accept `INPUT_REPO_ROOT`, `INPUT_VERSION_FILE`,
`INPUT_KANI_VERSION_FILE`, and `INPUT_VERSION` where Cyclopts can express those
bindings without ambiguous parameter ownership.

`prover-tools kani check-version` should support:

- `--repo-root PATH`.
- `--version-file PATH`.
- `--expected-version TEXT`.
- `--kani-command TEXT`, defaulting to `cargo kani`, with compatibility for
  `KANI`.

It must use `shlex.split` for `kani-command`, reject an empty command, append
`--version`, parse the first semantic version, compare it to the expected
version, and emit the same success meaning as the shell script.

`prover-tools verus install` should support:

- `--repo-root PATH`.
- `--version-file PATH`, defaulting to `tools/verus/VERSION`.
- `--checksum-file PATH`, defaulting to `tools/verus/SHA256SUMS`.
- `--target TEXT`, defaulting to `x86-linux`, with compatibility for
  `VERUS_TARGET`.
- `--install-dir PATH`, with compatibility for `VERUS_INSTALL_DIR`.
- `--base-url TEXT`, defaulting to the Verus GitHub release URL root.

It must preserve idempotence, checksum validation, archive extraction, and the
final installation message.

`prover-tools verus run` should support:

- `--repo-root PATH`.
- `--proof-file PATH`, defaulting to `verus/edge_harvest_proofs.rs`, with
  compatibility for `VERUS_PROOF_FILE`.
- `--version-file PATH`.
- `--install-dir PATH`, with compatibility for `VERUS_INSTALL_DIR`.
- `--verus-bin TEXT`, with compatibility for `VERUS_BIN`.
- `--ensure-toolchain / --no-ensure-toolchain`, defaulting to enabled.
- `--install-missing / --no-install-missing`, defaulting to enabled.
- `--extra-arg TEXT`, repeatable, for additional Verus arguments after the
  proof file if a requirement for passthrough flags is confirmed.

It must resolve the Verus binary using the same precedence as the shell script,
install the missing default binary when permitted, ensure the required Rust
toolchain when permitted, run Verus, and propagate verifier failure status.

## Implementation plan

Start by adding dependencies to `pyproject.toml`. Runtime dependencies should
include `cyclopts` and `cuprum`. Development dependencies should include
`cmd-mox`, `pytest-bdd`, `syrupy`, and `hypothesis`. Add CrossHair only if a
bounded model-checking target is included and its Python compatibility is
confirmed against the repository runtime.

Create these package modules:

```plaintext
rust_prover_tools/
  cli.py
  commands.py
  errors.py
  kani.py
  verus/
    __init__.py
    install.py
    models.py
    run.py
  versions.py
```

`cli.py` owns Cyclopts app construction, command registration, process exit
mapping, and user-facing error printing. It should expose `main() -> None` for
the console script.

`commands.py` owns the Cuprum catalogue, program constants, command builder
functions, command result helpers, and any wrappers needed to make CmdMox tests
use the same execution path as production.

`errors.py` defines small exception types for user-facing failures. Exceptions
should carry messages that can be printed directly to standard error without a
traceback for expected validation and command failures.

`versions.py` owns pure helpers for reading trimmed version files, validating
`MAJOR.MINOR.PATCH` versions, extracting a semantic version from tool output,
reading checksum rows, and parsing Verus toolchain output.

`kani.py` owns Kani install and version-check orchestration.

`verus/` owns Verus install, binary resolution, toolchain resolution, and
proof-run orchestration. Its `__init__.py` file is a compatibility facade for
the public functions and dataclasses used by the CLI and tests.

Create unit tests next to the package code, following the local Python rule
that unit tests for reusable code are colocated under an `unittests`
subdirectory:

```plaintext
rust_prover_tools/unittests/
  test_commands.py
  test_kani.py
  test_verus.py
  test_versions.py
```

Create behavioural and end-to-end tests under `tests/`:

```plaintext
tests/features/prover_tools_cli.feature
tests/steps/test_prover_tools_cli_steps.py
tests/test_prover_tools_cli_e2e.py
```

Use `cmd_mox` in tests that validate calls to `cargo`, `rustup`, `curl`,
`unzip`, `sha256sum`, `shasum`, and `verus`. Use local temporary directories
for version pins, checksum files, fake archives, and fake installed binaries.

Use Syrupy snapshots for CLI help, stable success lines, and stable error
messages where the exact output is part of the contract. Avoid snapshotting
absolute temporary paths unless the test normalizes them.

Use Hypothesis for pure helper invariants. At minimum, cover semantic-version
validation, checksum-row matching, and Verus toolchain parsing. Useful
properties include:

- valid version triples round-trip as accepted strings;
- arbitrary non-matching strings do not accidentally pass version validation;
- checksum lookup returns at most one hash for an archive name;
- toolchain parsing returns the same result for supported Verus version output
  forms regardless of surrounding lines.

Add package entry point configuration:

```toml
[project.scripts]
prover-tools = "rust_prover_tools.cli:main"
```

Update documentation after the behaviour is implemented. Add user-facing
command examples and environment variable reference material to
`docs/users-guide.md`. Add or update a design document that explains the
command architecture, Cuprum catalogue, and test-double strategy. Create
`docs/developers-guide.md` for internal conventions around adding future prover
commands, updating command catalogues, and writing CmdMox tests. Add an ADR if
the Cuprum/CmdMox decision needs durable rationale beyond the feature design.

## Validation plan

Before implementation, run the baseline where practical to know the starting
state:

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-rust-prover-tools-initial-tool-import-baseline.out
make typecheck 2>&1 | tee /tmp/typecheck-rust-prover-tools-initial-tool-import-baseline.out
make lint 2>&1 | tee /tmp/lint-rust-prover-tools-initial-tool-import-baseline.out
make test 2>&1 | tee /tmp/test-rust-prover-tools-initial-tool-import-baseline.out
```

After adding tests but before implementation, run the focused tests to confirm
the new tests fail for the expected reason. For example:

```bash
make test 2>&1 | tee /tmp/test-rust-prover-tools-initial-tool-import-red.out
```

After implementation, run focused tests first, then the full requested gates
sequentially:

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-rust-prover-tools-initial-tool-import.out
make typecheck 2>&1 | tee /tmp/typecheck-rust-prover-tools-initial-tool-import.out
make lint 2>&1 | tee /tmp/lint-rust-prover-tools-initial-tool-import.out
make test 2>&1 | tee /tmp/test-rust-prover-tools-initial-tool-import.out
```

If Markdown files change, also run:

```bash
make fmt 2>&1 | tee /tmp/fmt-rust-prover-tools-initial-tool-import.out
make markdownlint 2>&1 | tee /tmp/markdownlint-rust-prover-tools-initial-tool-import.out
make nixie 2>&1 | tee /tmp/nixie-rust-prover-tools-initial-tool-import.out
```

The final implementation is acceptable only when the requested gates pass, the
documentation-specific gates pass for changed Markdown, and the CLI behaviour
can be demonstrated with deterministic tests.

## Outcomes & Retrospective

The implementation is complete. The repository now provides the `prover-tools`
CLI, Kani and Verus workflow modules, deterministic unit, behavioural,
snapshot, property, and end-to-end tests, and the requested user, developer,
design, and ExecPlan documentation. CodeRabbit findings raised during
implementation were addressed until the user directed remaining polish to
normal code review. The final local gates passed and the change was committed.
