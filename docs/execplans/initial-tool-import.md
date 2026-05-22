# Convert prover shell scripts into one Cyclopts CLI

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: DRAFT

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

This plan is only a draft. Do not implement it until the user explicitly
approves the plan.

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
should use `shlex.split` to preserve quoted command arguments without invoking a
shell. Tests must cover quoted commands and empty commands.

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
- [ ] Await explicit user approval before implementation.
- [ ] Implement tests that describe the replacement CLI behaviour.
- [ ] Implement the CLI and internal command modules.
- [ ] Update user, developer, and design documentation.
- [ ] Run and record all required gates.
- [ ] Commit the approved, gated change.

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
project-specific Cuprum catalogue. This centralizes allowlisted programs such
as `cargo`, `rustup`, `curl`, `unzip`, `sha256sum`, and `shasum`, keeps command
assembly testable, and avoids accidental shell execution.

2026-05-22: Keep real network downloads out of the default test suite. The
Verus installer must be testable with local fixture archives and CmdMox
responses. Any true network end-to-end test should be opt-in and clearly marked
because normal `make test` must remain deterministic.

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
`tools/verus/SHA256SUMS`, uses `VERUS_TARGET` with default `x86-linux`, and
uses `VERUS_INSTALL_DIR` with default `.verus/<version>` under the repository
root.
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
  verus.py
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

`verus.py` owns Verus install, binary resolution, toolchain resolution, and
proof-run orchestration.

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

No implementation has started. The current outcome is a draft, self-contained
plan for review and approval.
