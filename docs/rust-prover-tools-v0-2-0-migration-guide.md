# rust-prover-tools 0.2.0 migration guide

This guide describes the change an existing `kani exec` user needs to know
about in rust-prover-tools 0.2.0. The change is additive for most callers:
repositories whose Cargo configuration selects a development codegen backend
now run instead of being refused. Callers that never selected one need no edit.
Readers new to the tool should start with the
[users' guide](users-guide.md#explicit-legacy-kani-execution) instead.

## What changed

Before 0.2.0, `kani exec` refused a repository whenever it found a
`codegen-backend` key in Cargo configuration or an ancestor workspace manifest.
The refusal was cautious but too broad: a repository that selects Cranelift for
its own development builds has no effect on the selected Kani release, because
Cargo lets an environment variable shadow the profile table.

In 0.2.0, `kani exec` classifies the key by its position. A string
`profile.<name>.codegen-backend` selection is accepted, then pinned to LLVM in
the child environment only:

```plaintext
CARGO_PROFILE_<NAME>_CODEGEN_BACKEND=llvm
```

Cargo gives that variable precedence over both configuration files and manifest
profiles, so the selected release keeps compiling with its own backend. The
caller's environment is unchanged, and the override does not outlive the
command.

The boolean `[unstable] codegen-backend = true` flag is also accepted. It
unlocks the profile option and selects no profile, so it needs no override.

## Accepted configuration

A repository that selects a development backend in `.cargo/config.toml` is
accepted as written:

```toml
[unstable]
codegen-backend = true

[profile.dev]
codegen-backend = "cranelift"
```

The same selection in an ancestor workspace `Cargo.toml` profile is accepted.
Profile names may contain only ASCII letters, digits, `-`, and `_`. Cargo
uppercases a name and maps `-` to `_`, so `release-lto` and `release_lto` share
`CARGO_PROFILE_RELEASE_LTO_CODEGEN_BACKEND`; both are pinned to LLVM.

## Configurations that remain refused

Two positions stay refused because the child environment cannot prove it
shadows them:

- A package-scoped `profile.<name>.package.<package>` override. Cargo resolves
  it above the profile variable, so the repository would still choose the
  backend.
- A name outside `[A-Za-z0-9_-]+`, whose environment spelling is not defined.

An unresolved `include` in Cargo configuration is still refused because it
hides inputs from validation. These refusals are unchanged from 0.1.0 and fail
before execution with a message naming the file and the key.

## Verifying the upgrade

Run the repository's proof once through the public command, from the proof
package rather than through `--manifest-path`:

```bash
prover-tools kani exec --installation "$HOME/.kani/kani-0.67.0" \
  --cwd "$PWD" --extra-arg=list
```

A successful run means the selected release compiled with its own backend. The
opt-in measurement module explains how to confirm that mechanism directly
against the bundled Cargo; see the
[developers' guide](developers-guide.md#legacy-kani-release-boundary).
