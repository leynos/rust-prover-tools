# 🦀 rust-prover-tools

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](
https://deepwiki.com/leynos/rust-prover-tools)

*One tidy command-line interface for installing and running the Kani and Verus
Rust provers.*

Pinning a prover is fiddly work: an exact release, a verified archive, a
matching helper toolchain, and the same setup on every machine. This package
collects that work into commands that behave the same on a laptop and in
Continuous Integration (CI).

______________________________________________________________________

## Why rust-prover-tools?

- **Pinned, not guessed**: prover versions come from files in the consuming
  repository (`tools/kani/VERSION`, `tools/verus/VERSION`), so every machine
  and every CI run installs the same build.
- **Verified downloads**: the Verus installer checks the release archive
  against `tools/verus/SHA256SUMS` before extracting it.
- **Repository-local installs**: Verus lands under `.verus/<version>` rather
  than a shared global toolchain, so projects can pin different releases.
- **One place to change**: workflows that used to live in shell scripts now
  live in a typed Python package with unit, behavioural, and end-to-end tests.

______________________________________________________________________

## Quick start

### Installation

`rust-prover-tools` requires Python 3.14 or later. Install it as an isolated
tool with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/leynos/rust-prover-tools
```

Check that the command is on the path:

```bash
prover-tools --help
```

Wheels are attached to each
[GitHub release](https://github.com/leynos/rust-prover-tools/releases) for
offline installs or for pinning a specific build.

### Basic usage

Run these commands from the root of a repository that pins its prover versions
under `tools/`. Install the pinned Kani release, then confirm that the
installed binary agrees with the pin:

```bash
mkdir -p tools/kani && echo 0.67.0 > tools/kani/VERSION

prover-tools kani install
prover-tools kani check-version
```

Install Verus, then verify a proof file:

```bash
prover-tools verus install
prover-tools verus run --proof-file verus/edge_harvest_proofs.rs
```

When the default Verus binary is missing, `verus run` installs it first, and it
installs the required Rust toolchain with `rustup` if `verus --version` reports
that one is needed. Verifier output is streamed to standard output, and a
failed proof exits with the verifier's own status.

______________________________________________________________________

## Features

- A small noun/verb surface: `kani install`, `kani check-version`,
  `verus install`, and `verus run`.
- `--repo-root PATH` on every command, defaulting to the current directory.
- `INPUT_`-prefixed environment variables for GitHub Actions-style wrappers.
- Legacy `KANI`, `VERUS_TARGET`, `VERUS_INSTALL_DIR`, `VERUS_BIN`, and
  `VERUS_PROOF_FILE` variables honoured for compatibility.
- Bounded download timeouts, so a stalled Verus download fails rather than
  hanging.
- No shell strings: user-supplied command text is parsed into an argument
  vector and executed directly.

______________________________________________________________________

## Learn more

- [Users' guide](docs/users-guide.md) — every command, option, and environment
  variable.
- [CLI design](docs/prover-tools-cli-design.md) — architecture, command
  construction, and test strategy.
- [Developers' guide](docs/developers-guide.md) — adding prover workflows and
  writing command tests.

______________________________________________________________________

## Licence

ISC — see [LICENSE](LICENSE) for details.

______________________________________________________________________

## Contributing

Contributions are welcome. Read [AGENTS.md](AGENTS.md) for the coding, testing,
and documentation conventions, then run `make all` before opening a pull
request.
