# Kani snapshot operations

This runbook is the operational handoff for the immutable Kani snapshot
distribution. It covers the guarded build and publication workflow, a
non-publishing validation run, the consumer verification of a produced release,
draft recovery, rollback, retention and recurring monitoring. It assumes the
reader has already read the
[snapshot design](kani-snapshot-distribution-design.md) and the
[execution plan](execplans/kani-monthly-snapshot-distribution.md).

Nothing in this document authorizes a publication or a repository setting
change. The first publication and the enabling of the schedule are the
separately authorized bootstrap in roadmap item 1.2.1, described in the
execution plan's EP-O1 milestone.

## Current state

As of 22 November 2026:

- The consumer commands `prover-tools kani snapshot install`, `snapshot check`
  and `kani run --snapshot-pin` are implemented and merged.
- The guarded producer workflow `.github/workflows/kani-monthly.yml` is merged
  at builder commit `6be9e71bc3ce98e96087be74b4cc572ca3488cb0`.
- A non-publishing run (run 45067145476) built, attested and validated a
  release with `publish=false`. Nothing was published, no tag was created and
  no repository setting changed.
- No snapshot has been published. The repository variable
  `KANI_SNAPSHOTS_ENABLED` is unset, so the monthly schedule is disabled.

## Trust model

A snapshot is trusted only when all of the following hold.

1. The run executed in `leynos/rust-prover-tools`, from `refs/heads/main`, for a
   `schedule` or `workflow_dispatch` event.
2. The build consumed the full upstream commit the pinned recipe names, and the
   checked-out tree was clean before and after the build.
3. Assembly created a build attestation through GitHub OpenID Connect (OIDC)
   and recorded its run and attempt in the manifest.
4. The validation job re-read every manifest and payload byte on a clean
   runner before writing a same-run receipt.
5. Publication is possible only from a validation receipt for that exact run
   and attempt, and only under a new immutable identity.

The consumer repeats the authenticated part of that chain: it checks the
manifest digest against the reviewed pin, the checksums against the manifest,
the attestation against the pinned signer workflow and source, and every
payload against its recorded size and digest.

## Required repository settings and permissions

The workflow declares `permissions: {}` at the top level. Each job receives
only what it needs:

| Job        | Permissions                                                                 |
| ---------- | --------------------------------------------------------------------------- |
| `guard`    | none                                                                        |
| `resolve`  | `contents: read`                                                            |
| `build`    | `actions: read`, `contents: read`                                           |
| `assemble` | `actions: read`, `attestations: write`, `contents: read`, `id-token: write` |
| `validate` | `actions: read`, `contents: read`                                           |
| `publish`  | `actions: read`, `contents: write`                                          |

_Caption: job permissions in `.github/workflows/kani-monthly.yml`._

The operator must confirm:

- GitHub Actions is enabled for the repository.
- Actions may create attestations and request an OIDC token (`id-token: write`
  and `attestations: write` are granted per job, not repository-wide).
- The `main` branch is protected and requires review, so the builder commit
  approved for a run is the commit that executes.
- The repository variable `KANI_SNAPSHOTS_ENABLED` exists and is either unset
  (disabled) or, after the separately authorized bootstrap, the exact string
  `true`. Any other value leaves the schedule disabled.
- The operator's `gh` can dispatch workflows and read Actions artefacts.
- The consumer host has the GitHub CLI with `gh attestation verify`, because
  the installer bundles no verifier of its own. A `PATH` entry another user can
  write could replace `gh`, so the install must run with a trusted `PATH`.

## Schedule and dispatch inputs

The workflow has one schedule and one manual trigger:

```yaml
on:
  schedule:
    - cron: "23 4 3 * *"
  workflow_dispatch:
    inputs:
      upstream_commit:
        required: true
        type: string
      publication_id:
        required: true
        type: string
      publish:
        required: false
        default: false
        type: boolean
```

_Caption: trigger declaration in `.github/workflows/kani-monthly.yml`._

A manual dispatch takes three inputs:

| Input             | Meaning                                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `upstream_commit` | Full 40-hex `model-checking/kani` commit to build. It must equal the pinned recipe's commit; another commit is refused at plan time. |
| `publication_id`  | The immutable tag for this run: `kani-snapshot-YYYY-MM-<12 hex upstream prefix>-rN`.                                                 |
| `publish`         | `false` builds, attests and validates without creating a release. Defaults to `false`.                                               |

_Caption: manual dispatch inputs._

A dispatch always creates a build attestation with OIDC, even when `publish` is
`false`. A non-publishing dispatch is therefore a complete dry run of the trust
chain without making anything public.

## Identity allocation

The tag has four fields:

- `kani-snapshot-`: the immutable namespace.
- `YYYY-MM`: the publication month in UTC.
- `<12 hex>`: the first 12 characters of the upstream commit. This is a
  binding, not a free label; `make kani-snapshot-smoke` refuses a tag that
  disagrees with `KANI_SNAPSHOT_UPSTREAM_COMMIT`.
- `rN`: the revision, starting at 1, for a new build of the same month and
  upstream commit.

A scheduled run allocates the next revision from the identities the repository
has already published. A manual dispatch supplies its identity. Neither path
may reuse a published identity or move an existing tag. A changed recipe,
toolchain, builder commit or other recorded input receives a new revision; the
input fingerprint, not the builder commit alone, decides whether an existing
revision may be reused.

## Dispatch a non-publishing validation run

Use the Makefile target, which validates its inputs before it reaches `gh`:

```sh
make kani-snapshot-smoke \
  KANI_SNAPSHOT_UPSTREAM_COMMIT=0d2328a93f0e0ff66132d6bfa1a7d884877cf862 \
  KANI_SNAPSHOT_PUBLICATION_ID=kani-snapshot-2026-11-0d2328a93f0e-r1
```

The target passes `publish=false` and never publishes. It runs only from a
checkout whose `origin` can dispatch the canonical repository; the workflow's
own guard refuses another repository, ref or event.

The run has these jobs:

1. `guard` checks the repository, ref and event.
2. `resolve` confirms the pinned upstream commit and the named checks, then
   records the publication identity.
3. `build` checks out the upstream commit, verifies the tree is clean, builds
   the bundle and launcher, measures the inventory, and records wall time and
   disk high-water use.
4. `assemble` stages the payloads, writes `manifest.json` and `SHA256SUMS`, and
   attests the manifest.
5. `validate` re-verifies every byte on a clean runner and uploads a same-run
   receipt.
6. `publish` is skipped when `publish` is `false`.

Expected artefacts, retained for seven days:

- `kani-snapshot-build-<run>-<attempt>`
- `kani-snapshot-resolution-<run>-<attempt>`
- `kani-snapshot-release-<run>-<attempt>`
- `kani-snapshot-validation-<run>-<attempt>`

Download the release and validation artefacts into a new, empty directory:

```sh
mkdir -p "$HOME/kani-validation/release"
gh run download <run-id> --repo leynos/rust-prover-tools \
  -n kani-snapshot-release-<run-id>-1 -D "$HOME/kani-validation/release"
```

Do not download artefacts from another run, and do not relabel a failed run's
partial output as evidence.

## Consumer verification of a produced release

The release directory holds `manifest.json`, `manifest.sigstore.jsonl`,
`SHA256SUMS`, `inventory.json`, the backend bundle, the launcher bundle and the
notices archive. Verification installs it through the public commands in a
fresh virtual environment, with no upstream checkout and no host Kani
installation in the path.

Prerequisites:

- The built consumer wheel
  (`uv build --wheel --out-dir "$HOME/kani-validation/dist"`).
- `gh` on `PATH`, authenticated enough to verify a public attestation.
- Network access for `gh attestation verify` and the pinned Rust toolchain
  download.
- `strace`. The target requires it: without a trace the reuse, refusal and
  clean-setup claims cannot be observed, so the target fails early instead of
  reporting an unproven pass.
- A Rust 1.93 compiler for the below-floor control. Resolve it explicitly:

```sh
KANI_SNAPSHOT_RUSTC_1_93="$(rustup which --toolchain 1.93.0 rustc)"
```

Run the verification target:

```sh
make kani-snapshot-verify \
  KANI_SNAPSHOT_ASSET_DIR="$HOME/kani-validation/release" \
  KANI_SNAPSHOT_WHEEL="$HOME/kani-validation/dist/rust_prover_tools-0.1.0-py3-none-any.whl" \
  KANI_SNAPSHOT_RUSTC_1_93="$KANI_SNAPSHOT_RUSTC_1_93"
```

The target builds a throwaway virtual environment, installs the wheel into it,
and runs only `tests/integration/test_kani_snapshot_smoke.py`. It never
publishes and is never part of `make all`.

The verification requires:

- A cold `snapshot install` that names the pin's tag and the observed compiler.
- The pinned launcher's own `--version` naming the manifest's backend version.
- `cargo kani` running upstream's TestBundle crates under the generation's own
  environment: `simple-lib`, `build-rs-works` and `simple-kissat`, reproduced
  verbatim, so the library, build-script and external-Kissat-solver routes are
  all exercised.
- An effective compiler at or above Rust 1.94, parsed from the install's
  `rustc:` report.
- `snapshot check` accepting the generation.
- Each named positive harness verifying in its own transcript:
  `bounded_push_contents_are_exact` and `raii_release_runs_once`.
- `deliberately_false_assertion` failing verification, not compilation, and
  naming `Failed Checks: deliberately false`.
- `bounded_adjacent_pairs_are_exact` verifying under the pinned compiler, with
  the exact named set and summary.
- The required Rust 1.93 control reporting a version below 1.94 and failing
  with `error[E0658]` for `array_windows`.
- A warm repeat reusing the same generation, proven by comparing the reported
  generation path and by a trace with no `cargo-kani setup`.
- A corrupted backend payload refused, with no generation published.
- A pin naming another tag refused before setup.
- A setup trace with no source compilation: no `cargo`, no code-generating
  `rustc`, and no C/C++ compiler, linker or build driver.

Record the run URL, publication identity, upstream and builder commits, the
observed compiler, the harness names and statuses, and the two refusal cases in
the execution plan's Progress section. Keep the full logs under `/tmp` or the
validation directory; record only bounded transcripts in the plan.

## Cold public installation after publication

After the separately authorized first publication, a consumer installs from the
release the reviewed pin names, without `--asset-dir`:

```sh
prover-tools kani snapshot install --repo-root /path/to/consumer
prover-tools kani snapshot check --repo-root /path/to/consumer
prover-tools kani run --repo-root /path/to/consumer \
  --snapshot-pin tools/kani/snapshot.toml -- --harness bounded_push
```

The install verifies the manifest digest against the pin, the checksums against
the manifest, the attestation against the signer workflow and source digests,
and every payload against its recorded digest. It then runs the packaged
launcher's prebuilt setup and probes the installed compiler before activating
the generation. The recorded component digest covers the sorted component
names, so rustup's nondeterministic write order does not affect it; an added,
removed or renamed component is still refused. Repeating the install reuses a
validated generation; a failed install leaves the previous generation and
activation record unchanged.

## Draft recovery

A publication interrupted after the draft was created resumes only for the same
run and attempt. The publisher re-observes the draft before writing:

- An asset GitHub reports with a digest is downloaded and checked against the
  expected bytes.
- An empty record left by a failed upload is removed and uploaded again only
  after that check.
- A draft that records another run or attempt, or that was retargeted, is
  refused; do not adopt it.
- Missing assets may be added; existing bytes are never replaced.

If recovery is refused, inspect the release with `gh release view <tag>` and
choose a new publication identity rather than deleting or rewriting a tag.

## Rollback

Rollback is a reviewed older pin. Move the consumer's
`tools/kani/snapshot.toml` back to an earlier published identity, reinstall,
and run `snapshot check` and the proof. Never move or delete a published tag,
never re-upload asset bytes, and never edit a release in place. A bad snapshot
is superseded by a new revision, not repaired under the same identity.

## Retention

- Workflow artefacts are retained for seven days. The release assets are the
  durable record; download any diagnostic artefact you need before it expires.
- Published releases are immutable and retained indefinitely. Do not delete a
  historical snapshot: consumers may pin it.
- Snapshot cache generations on a consumer host are never deleted by an
  install. A corrupt or superseded generation may be removed only by an
  operator who has confirmed that no consumer pin and no running proof uses it.

## Monitoring

Assign one owner to review the monthly run. GitHub schedules can be delayed or
dropped, run from the default branch, and are disabled after prolonged
inactivity in public repositories.[^1]

The monthly checklist:

1. Confirm a `Kani snapshot` run completed for the month, from `refs/heads/main`
   and the canonical repository.
2. Confirm the build job's wall time and disk high-water mark remain within the
   measured envelope, and that the cache outcome is recorded.
3. Confirm the validation receipt names the same run and attempt.
4. Confirm the published release is a pre-release, not `latest`, and that its
   attestation verifies.
5. Confirm the next consumer pin still installs and proves.

If a month is missed:

1. Check the Actions tab and the run history before assuming a failure.
2. If scheduling was disabled through inactivity, re-enable it only as part of
   the authorized operational process.
3. Dispatch a manual run for the missed month with the pinned upstream commit
   and an unused identity, with `publish=false` first. Inspect the evidence,
   then dispatch the authorized publication with a new identity if the build
   inputs or run changed.

## Source pins and runtime floor

| Component              | Pin                                                           |
| ---------------------- | ------------------------------------------------------------- |
| Upstream Kani          | `kani-0.68.0` at `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`   |
| Rust toolchain         | `nightly-2026-08-21` (reported `1.100.0-nightly`)             |
| Supported floor        | Rust 1.94, demonstrated by `slice::array_windows`             |
| CBMC                   | 6.11.0, checksum-pinned                                       |
| Kissat                 | 4.0.1, built from the pinned source during the producer build |
| Builder image          | `ubuntu-22.04`                                                |
| Supported target       | `x86_64-unknown-linux-gnu`                                    |
| Python for the package | 3.14                                                          |

_Caption: pinned components and the supported floor._

The runtime floor is a property of the pinned compiler, not of the builder
image: the snapshot's compiler is the upstream nightly, and its `rustc` must
report at least 1.94 for the floor harness to compile. The producer build
compiles Kani, Kissat and the launcher once; a consumer setup downloads the
exact prebuilt Rust toolchain that the bundle pins and compiles no tool source.

## Limitations

- Only `x86_64-unknown-linux-gnu` is supported.
- The schedule relies on GitHub's scheduler. It is not a hard real-time
  guarantee, and a run can be delayed or dropped.
- OIDC attestation verification requires GitHub's attestation service; local
  `act` runs cannot certify hosted permissions or signatures.
- The consumer proof is not a sandbox. The selected Kani build and the proof
  workspace must be trusted.
- The repository-owned fixtures demonstrate the distribution and the compiler
  floor; they do not establish full-crate compatibility for every consumer.

## Troubleshooting

| Symptom                                                     | Meaning                                                                        | Action                                                                               |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| `snapshot tag prefix must match the pinned upstream commit` | The tag's 12-hex field is not the commit prefix.                               | Recompute the tag; `make kani-snapshot-smoke` refuses it locally.                    |
| `snapshot manifest digest does not match the reviewed pin`  | The pin names other manifest bytes.                                            | Re-review the release and pin, or use the release the pin names.                     |
| `gh attestation verify` fails                               | The bundle is absent, for another run, or from another signer.                 | Use the same-run attestation; check the policy repository, ref and digests.          |
| Setup reports `rustup is not installed at .../cargo-home`   | The host rustup tried to self-update against the private cargo home.           | Update the package; setup disables auto-self-update before the launcher runs.        |
| `snapshot check` reports a missing or changed runtime file  | The generation drifted or was damaged.                                         | Reinstall from the verified assets; do not edit the generation.                      |
| A proof reports `CBMC failed`                               | The solver failed; this is not a proof result.                                 | Inspect the transcript and the recorded bundle identity; rerun only after diagnosis. |
| A run shows `trusted=false`                                 | The schedule is disabled, or the run was not the canonical repository and ref. | Check `KANI_SNAPSHOTS_ENABLED` and the event; do not widen the guard.                |

_Caption: common failures and their remedies._

## References

- [Kani snapshot distribution design](kani-snapshot-distribution-design.md)
- [Monthly snapshot execution plan](execplans/kani-monthly-snapshot-distribution.md)
- [Users' guide: Kani snapshot commands](users-guide.md#kani-snapshot-commands)
- [Developers' guide: Kani snapshot installation](developers-guide.md#kani-snapshot-installation-and-execution)

[^1]: [GitHub workflow schedule events](https://docs.github.com/en/actions/reference/workflows-and-events/events-that-trigger-workflows#schedule).
