# Development roadmap

This proposed roadmap is narrowly scoped to monthly Kani snapshots. No roadmap
existed in the planning baseline. The identifiers below are new proposals, not
previously assigned work. All entries remain open until their acceptance
evidence exists; plan approval and merging documentation do not complete them.

## 1. Immutable Kani snapshot distribution

Provide opt-in prebuilt Kani snapshots for `x86_64-unknown-linux-gnu`,
preserving the legacy release path. See the
[snapshot design](kani-snapshot-distribution-design.md) and
[execution plan](execplans/kani-monthly-snapshot-distribution.md).

### 1.1. Implement and verify distribution capability

- [ ] 1.1.1. Establish the upstream bundle and compatibility recipe. Complete
  EP-M1 with pinned upstream evidence, local-bundle setup, matching launchers,
  runtime/licence inventory, and a demonstrated compiler floor of Rust 1.94.
- [ ] 1.1.2. Define validated pins and authenticated manifests. Requires 1.1.1.
  Complete EP-M2 with schema, selector, digest, and attestation-policy tests.
- [ ] 1.1.3. Deliver isolated binary installation and execution. Requires 1.1.2.
  Complete EP-M3 with deterministic CLI, HTTP, archive, concurrency, cache,
  identity, and same-semantic-version regression evidence.
- [ ] 1.1.4. Deliver guarded build and publication orchestration. Requires
  1.1.3. Complete EP-M4 with tested Python helpers, monthly/manual workflow,
  publication recovery tests, and the necessary Python release lifecycle fix.
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
