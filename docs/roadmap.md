# Development roadmap

This roadmap is narrowly scoped to monthly Kani snapshots. Its execution plan
was approved on 9 November 2026. All entries remain open until their acceptance
evidence exists; plan approval and merging code do not complete them on their
own.

## Status, 19 November 2026

- **Merged:** the EP-M2 and EP-M3 implementations, in #131 (14 November): pin
  and manifest contracts, and isolated snapshot installation, identity checking
  and execution. The installer downloads the release its pin names, with a local
  `--asset-dir` retained as an offline override.
- **Completed (15 November):** EP-M1, the upstream recipe and the demonstrated
  Rust 1.94 compiler floor. The released `kani-0.68.0` tag is pinned, the
  producer recipe and its evidence tests are in place, and the prototype bundle
  built, installed through local setup and verified the floor harness.
- **In review:** EP-M4 (build and publication), in #258. The schedule stays
  disabled, so no snapshot has been published.
- **Not started:** EP-M5 (smoke build and handoff) and the first live
  publication. This project has not published a snapshot.

## 1. Immutable Kani snapshot distribution

Provide opt-in prebuilt Kani snapshots for `x86_64-unknown-linux-gnu`,
preserving the legacy release path. See the
[snapshot design](kani-snapshot-distribution-design.md) and
[execution plan](execplans/kani-monthly-snapshot-distribution.md).

### 1.1. Implement and verify distribution capability

- [x] 1.1.1. Establish the upstream bundle and compatibility recipe. Complete
  EP-M1 with pinned upstream evidence, local-bundle setup, matching launchers,
  runtime/licence inventory, and a demonstrated compiler floor of Rust 1.94.
  Completed 15 November. EP-M1 is recorded in
  [the execution plan](execplans/kani-monthly-snapshot-distribution.md).
- [x] 1.1.2. Define validated pins and authenticated manifests. Requires 1.1.1.
  Complete EP-M2 with schema, selector, digest, and attestation-policy tests.
  Implementation and tests merged in #131; the hold on 1.1.1 was released on 15
  November.
- [x] 1.1.3. Deliver isolated binary installation and execution. Requires 1.1.2.
  Complete EP-M3 with deterministic CLI, HTTP, archive, concurrency, cache,
  identity, and same-semantic-version regression evidence. Implementation
  merged in #131, including the same-reported-version regression test
  (`test_same_reported_version_installs_into_disjoint_generations`); 1.1.1 and
  1.1.2 were completed on 15 November.
- [x] 1.1.4. Deliver guarded build and publication orchestration. Requires
  1.1.3. Complete EP-M4 with tested Python helpers, monthly/manual workflow,
  publication recovery tests, and the necessary Python release lifecycle fix.
  Implementation and tests are in #258. The schedule stays disabled until EP-M5
  records real smoke evidence and EP-O1 grants operational bootstrap.
- [ ] 1.1.5. Validate clean proofs and document operational handoff. Requires
  1.1.4. Complete EP-M5 with a non-publishing smoke build, public consumer-path
  installation, named positive/negative harnesses, compiler-floor evidence,
  warm-cache repeat, integrity failures, and passing gates. Mark only completed
  implementation entries done after explicit approval and implementation.

### 1.2. Bootstrap live operation separately

- [ ] 1.2.1. Publish and independently verify the first immutable snapshot.
  Requires 1.1.5 and separate operational authorization. Record actual
  settings, run/attempt, immutable pre-release URL, verified provenance, and a
  successful cold install/proof from public assets; enable the schedule only
  afterwards. Assign monitoring and manual missed-run recovery. This entry
  stays open when implementation merges: code completion is not the first
  publication.

## 2. Supervised Kani harness execution

Provide a reusable, inspectable per-harness execution capability that survives
runner loss. See the
[supervised execution reference](kani-supervised-execution.md) and
[issue #260](https://github.com/leynos/rust-prover-tools/issues/260).

### 2.1. Upstream per-harness observability and inspectability

- [x] 2.1.1. Deliver inventory, supervision, evidence, and reporting. Complete
  a strict package-qualified inventory with `#[path]` and inline-module
  discovery, one exact invocation per harness in an owned process group, live
  human and JSONL evidence, process-group RSS and host-memory sampling, a
  timeout and host-memory watchdog, typed outcomes and multiset accounting, and
  versioned JSONL and report schemas. Exposed through a typed Python API and
  `kani harnesses list`/`run`. Delivered; the final gate reports 3,341 passed
  and 6 skipped at code head `723f19a`.
- [ ] 2.1.2. Adopt the capability in Cuprum. Requires 2.1.1. Wire the
  package-specific Make targets, CI matrix split, fault-check dependencies, the
  19-harness inventory, and any proof repairs in
  [cuprum#723](https://github.com/leynos/cuprum/issues/723); do not encode
  Cuprum's layout or inventory here.
