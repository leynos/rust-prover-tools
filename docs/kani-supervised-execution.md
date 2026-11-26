# Kani supervised harness execution

## Status and scope

Status: implemented. This document is the design and reference for the reusable
supervision capability added for
[rust-prover-tools#260](https://github.com/leynos/rust-prover-tools/issues/260)
and the Cuprum plan in
[cuprum#723](https://github.com/leynos/cuprum/issues/723).

The capability discovers every Kani proof in selected Cargo packages, runs one
exact verifier invocation per harness in its own process group, streams live
evidence, samples resource use, enforces a wall-clock budget and a host
available-memory floor, classifies each termination, and writes a versioned
report. It is exposed through a typed Python API and the
`prover-tools kani harnesses` commands.

Kani 0.67.0 is the only registered integration. The supervision, measurement,
evidence, accounting, and reporting layers are prover-neutral; an adapter for
another prover supplies only its observer and classifier.

## Why supervised execution

A package-wide verifier invocation reports one result for many harnesses. It
cannot say which harness was running when a runner was lost, which one grew
without bound, or how much host memory remained. A runner that is killed for
memory produces no end-of-job artefact at all, so the only evidence that
survives is what was already flushed into the job log.

One invocation per harness changes that. Each harness gets its own process
group, its own budget, its own sampled memory, its own termination reason, and
its own evidence records. A failure in one harness does not hide the evidence
about the others, and a host-memory watchdog can turn a runner loss into an
attributable policy stop before the runner disappears.

## Quick start

List the resolved inventory before running anything:

```bash
prover-tools kani harnesses list --repo-root . --package cuprum-native-io
```

Run every discovered harness against an installed qualified release:

```bash
prover-tools kani harnesses run --repo-root . \
  --package cuprum-native-io \
  --installation "$HOME/.kani/kani-0.67.0" --cwd . \
  --jsonl target/kani/evidence.jsonl --report target/kani/report.json
```

Run through an immutable snapshot instead:

```bash
prover-tools kani harnesses run --repo-root . \
  --snapshot-pin tools/kani/snapshot.toml --cache-dir .kani-snapshots \
  --package cuprum-native-io
```

The snapshot route accepts only a snapshot that reports Kani 0.67.0, because
that release is the one registered grammar. The current snapshot recipe builds
the released `kani-0.68.0` tag, whose banner the parser refuses; that snapshot
is not usable with this command until a 0.68.0 grammar is registered. Use the
installed-release route above for a 0.67.0 release.

The `run` command exits zero only for a complete passing run, one for any other
completed run or expected error, and `128 + signal` when a signal interrupted
the run. An execution-phase interruption still finalizes the report;
preparation ends immediately because no supervised child exists yet.

## Configuration

The defaults are the Cuprum source plan's proposed starting settings. They are
policy, not fact; every one is configurable.

_Table 1: Supervised-execution defaults._

| Setting                          | Default         | CLI flag                          |
| -------------------------------- | --------------- | --------------------------------- |
| Per-harness wall-clock budget    | 1,200 seconds   | `--timeout-seconds`               |
| Resource sampling interval       | 5 seconds       | `--sample-interval-seconds`       |
| Silent-prover heartbeat interval | 15 seconds      | `--heartbeat-seconds`             |
| Host available-memory floor      | 1,024 MiB       | `--watchdog-mib`, `--no-watchdog` |
| Human evidence destination       | standard output | `--human PATH`                    |
| JSONL evidence destination       | disabled        | `--jsonl PATH`                    |
| Final JSON report                | disabled        | `--report PATH`                   |

A zero or negative interval is refused at construction. A disabled watchdog is
represented by `None` in the library and by `--no-watchdog` in the CLI.

## Inventory discovery

Discovery resolves Cargo packages with
`cargo metadata --no-deps --format-version 1`, then scans each selected
package's `lib`, `bin`, and `test` targets. `proc-macro`, `example`, `bench`,
and `custom-build` targets cannot carry harnesses and are ignored.

The source scan is lexical rather than syntactic. It blanks comments, normal
strings, raw strings, byte strings, and character literals before looking for
items, so a proof attribute in a doc example is not a proof. Lifetimes are left
untouched because they are code.

The scan understands exactly what discovery needs:

- `#[kani::proof]` and `#[kani::proof_for_contract]`, directly or through a
  `cfg_attr` that is conditional unless its predicate is exactly `kani`;
- inline modules, which extend the module path;
- `mod name;` declarations, resolved by the Rust 2018 rules: a module declared
  in `lib.rs`, `main.rs`, or a `mod.rs` file looks beside that file, and a
  module declared in `foo.rs` looks under `foo/`;
- `#[path = "..."]` registration with an ordinary, raw, or hashed raw string
  value, resolved against the declaring file's directory (and, inside an inline
  module, under the inline module's name); a module loaded through it resolves
  its own children beside the selected file;
- `cfg` attributes, which are recorded but never evaluated.

A proof gated by `cfg(kani)` is the normal Kani activation and is listed
normally. A proof gated by any other `cfg` is listed with
`"conditional": true`; whether it exists in the selected build configuration is
a question only the build can answer. The run fails closed if Kani does not
select the identity the inventory names.

Discovery refuses an unknown package, a package with no verifiable target, a
package that resolves to no harnesses, a duplicate pretty name across targets,
a missing module file, an ambiguous pair of module files, and a module file
that re-enters an active ancestor (a cycle). One file reached through two
different `#[path]` declarations is allowed: the two module paths give it two
distinct identities.

### Inventory document, version 1

The strict document is the inspectable interface between discovery and
execution. Every key is required, no unknown key is permitted, an empty package
is refused, and a duplicate `package::harness` identity is refused.

```json
{
  "schema": 1,
  "packages": [
    {
      "package": "cuprum-native-io",
      "harnesses": [
        {
          "package": "cuprum-native-io",
          "target": "cuprum-native-io",
          "target_kind": "lib",
          "harness": "pump_machine_kani_proofs::pump_never_exceeds_capacity",
          "source": "src/pump_machine_kani_proofs.rs",
          "line": 42,
          "conditional": false,
          "attributes": ["kani::proof"]
        }
      ]
    }
  ]
}
```

`prover-tools kani harnesses list` prints this document. The `--inventory`
option loads it as the authoritative inventory and bypasses discovery; a
`--package` selection narrows it and fails if a requested package is absent.

## Exact selection and argument composition

A `--harness` filter without `--exact` is a substring match, and a bare name
can match several proofs. The supervisor therefore composes every invocation as:

```plaintext
--package <package> --harness <pretty-name> --exact \
  <--lib | --bin <target> | --tests> \
  --output-format=regular --jobs=1 <caller arguments>
```

The selector comes first, the registry's output argument comes next, and caller
arguments are preserved verbatim after it. A caller argument that spells a
selector or output override is refused, in separate-value form
(`--harness value`), joined form (`--harness=value`), or short-option form
(`-pvalue`), on either side of `--`. Proof-affecting flags such as `--unwind`,
`--default-unwind`, `-Z`, and `--fail-fast` are preserved.

A pinned inventory is revalidated against a fresh discovery of the tree the run
will build. A test-target entry whose harness was removed, renamed, moved to
another target, or is gated by a `cfg` other than `cfg(kani)` is refused, and a
package that declares more than one integration-test target is refused
outright: a `cfg`-disabled harness can be absent while a same-named generated
harness elsewhere is selected, so lexical discovery alone cannot attribute it.

Cargo target metadata the run does not evaluate is refused rather than assumed
benign. A selected test target whose Cargo metadata declares
`required-features` is refused, because this run does not enable those features
and Cargo may skip the target: a pinned harness the verifier never builds
cannot be attributed to the run, even though the result would be an error
rather than a false pass.

The refusal above rests on measured Kani 0.67.0 behaviour, not on inference.
The three captures are committed under `tests/fixtures/kani_collision/` and are
not parsed by any gate. In a fixture package whose integration test declared
`required-features`, `cargo kani --tests` aborted with "target `integration` in
package … requires the features" and produced no verification record at all.
With the same `required-features` removed and the test copy disabled by `cfg`,
the library copy of the identical pretty name was the only record, and it
passed; Kani never reported the absent test copy. With both copies compiled,
Kani printed two records for the one name and reported one success and one
failure; the supervisor's live selection facts then hold two identities, which
classify as `multiple-match` rather than crediting either, and the shared
parser refuses the finished log as a harness reported twice.

Kani 0.67.0 has no flag that selects one integration-test target. Its `--tests`
option enables verification of test crates _and_ leaves the library and every
binary eligible, so a harness name alone does not say which test target
produced a record. Supervision therefore refuses a selected test-target harness
when its package declares more than one integration-test target: the same
pretty name could belong to another target and be credited to the pinned one.
With exactly one test target, a declared harness that no other target also
declares is attributed to it: discovery already refuses a package whose pretty
name appears in more than one target. A same-named harness in another package
does not silently stand in for it either. Measured with Kani 0.67.0, two
compiled copies of one pretty name produce two selection records, which
supervision classifies `multiple-match`, and the shared parser refuses the
finished log. Lib and bin selectors name their target (`--lib`, `--bin <name>`)
and need no such check.

Selection refuses a bare name that matches more than one package, a name the
inventory does not declare, and a duplicate request. A package-qualified
identity such as `cuprum-native-io::pump::proof` disambiguates two packages
that use the same leaf name.

## Execution model

Harnesses run sequentially. Each invocation is launched through Cuprum in its
own process group, with capture disabled and a bounded line observer. One
monitor task owns every timing decision: it samples resource use, emits
heartbeats, checks the wall-clock budget, checks the host-memory floor, and
requests the first stop that applies. Cancelling the launcher task makes Cuprum
settle the owned group before its cancellation surfaces, so every descendant
that remains in that group is terminated with the child that started it. A
descendant that creates or joins another process group is outside this
guarantee.

The attempt owns the child until it has settled. A stop, a monitor failure, or
a cancellation of the supervising coroutine all stop the child when nothing
else has and then join its launch task before reporting or propagating. A
cancelled run task accounts for every harness without a result as `not-run` and
publishes the available report before the cancellation continues, so a caller
that cancels a run still gets the evidence collected so far.

A failure, timeout, or watchdog stop does not prevent later harnesses from
running. Only an interruption stops the sequence; the active harness is
reported as interrupted and every harness that never ran is reported as
`not-run`.

## Outcomes and precedence

_Table 2: Outcome vocabulary._

| Status             | Meaning                                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------------------- |
| `passed`           | Exactly one exact selection record, a successful verdict, a reconciled manual summary, and exit zero. |
| `failed`           | A failed proof verdict, including a failed check that is not an expected panic.                       |
| `no-match`         | No selection record, or one record naming a different harness.                                        |
| `multiple-match`   | More than one selection record.                                                                       |
| `timed-out`        | The supervisor's wall-clock budget expired.                                                           |
| `memory-exhausted` | The supervisor's host-memory watchdog stopped the harness.                                            |
| `interrupted`      | A signal stopped the harness.                                                                         |
| `exit-nonzero`     | The child exited nonzero or by signal with no stronger evidence.                                      |
| `error`            | A launch failure or malformed verifier output.                                                        |
| `not-run`          | The run stopped before this harness.                                                                  |

Classification applies one precedence, strongest evidence first:

1. a supervisor stop (timeout, watchdog, interruption);
2. a launch failure;
3. a selection mismatch;
4. malformed or unattributable verifier output;
5. a failed proof verdict;
6. a nonzero or signalled exit;
7. success.

A low-memory watchdog stop is an observed policy intervention. It is never
presented as proof that the kernel killed the process for memory. When the
cgroup evidence provides an `oom_kill` counter delta, the delta is recorded
separately in the report and in the detail text.

A stop outranks the status, but not evidence the verifier already produced: a
harness that reported a verdict before the supervisor ended it keeps that
verdict in `proof_verdict` while its status records the stop. A stop the
supervisor itself causes, because its monitor failed or its caller cancelled
it, is recorded as `interrupted`, and that attempt keeps its diagnostic
reference and emits its own finish record.

A child that exits nonzero without selection evidence is classified
`exit-nonzero`, not `no-match`: a banner proves only that the verifier started.
`no-match` requires a completed report, verdict records, or Kani's own "no
harnesses matched" diagnostic.

## Evidence

Two destinations receive the same records:

- a human job log, flushed per line, written to standard output by default or
  to `--human PATH`;
- versioned JSONL, flushed per line, written to `--jsonl PATH`.

Both are bounded. A human line is cut to the configured `max_line_bytes` with
an explicit truncation marker. A JSONL record that exceeds the budget is not
cut mid-JSON: its large string values are shortened until the serialized object
fits, and the shortened values carry the marker, so every emitted line remains
valid JSON. If even that cannot fit, a minimal `truncated` record states the
original kind and the marker. Nothing retains the whole proof log. Each harness
result carries a diagnostic reference to the absolute byte offset where that
harness's JSONL evidence begins, including when the file is appended to across
runs.

Supervision does not echo. It borrows the child's stdout and stderr pipe ends
through Cuprum's `StdioTarget.fd`, reads them in 64 KiB chunks on the event
loop, and splits lines itself. The splitter caps the partial line at
`max_line_bytes`; a longer newline-free line is forwarded truncated with an
explicit marker and the rest of it is discarded until its terminator. Line
endings follow Cuprum's CR, LF, and CRLF rules, and a CR that ends one chunk
waits for the next chunk before it is treated as a boundary. Each complete line
is decoded independently, so an incomplete byte sequence becomes the
replacement character at the boundary where it occurs rather than leaking into
the next line or disappearing at end of stream.

The bound is therefore end to end for everything the supervisor retains: a
partial line, a forwarded line, a JSONL record, a stream tail, and a report are
each bounded. `test_a_newline_free_line_is_bounded_end_to_end` drives a real 8
MiB newline-free line through the production launcher and asserts that no
forwarded line exceeds the configured bound.

The final parse uses the repository's single Kani parser. The observer spools
the child's lines to a temporary file up to `max_log_bytes` (64 MiB by default,
or `--max-log-mib` on the CLI) and then parses that file through
`parse_kani_log_lines`, whose streaming line source retains only the small
lookahead the parser needs, refuses a single line beyond `MAX_LINE_CHARS` (4 Mi
characters) and refuses to retain more than `MAX_BUFFERED_CHARS` (8 Mi
characters) across the window. No whole log is held in memory; a log longer
than the budget is reported as truncated evidence. Cuprum's own line hook
retains an unterminated line before invoking the hook, which is why supervision
does not use it; that remains true of Cuprum 0.2.0b2 and is recorded here as
historical context, not as an active limitation of this route.

### JSONL records, version 1

Every record carries `"schema": 1`, a `record` kind, and a `run_id`. Harness
records also carry `package`, `harness`, and `attempt`.

_Table 3: JSONL record kinds._

| Kind             | Additional fields                                                                                                                                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run-start`      | `timestamp`, `packages`, `harness_count`, `timeout_seconds`, `sample_interval_seconds`, `heartbeat_interval_seconds`, `watchdog_mib`                                                                              |
| `harness-start`  | `timestamp`, `elapsed_seconds`                                                                                                                                                                                    |
| `output`         | `timestamp`, `elapsed_seconds`, `stream`, `text`                                                                                                                                                                  |
| `sample`         | `timestamp`, `elapsed_seconds`, `group_rss`, `host_available`, `host_total`                                                                                                                                       |
| `heartbeat`      | `timestamp`, `elapsed_seconds`, `idle_seconds`, `group_rss`, `host_available`, `last_progress`                                                                                                                    |
| `harness-finish` | `timestamp`, `status`, `stop_reason`, `exit_code`, `signal_number`, `proof_verdict`, `duration_seconds`, `peak_group_rss`, `min_host_available`, `host_total`, `cgroup`, `last_output`, `last_progress`, `detail` |
| `run-finish`     | `timestamp`, `outcome`, `complete`, `interrupted`, `interrupt_signal`, `accounting`                                                                                                                               |

A measurement is exactly one of `{"value": <bytes>}` or
`{"unavailable_reason": "<why>"}`. `rust_prover_tools.supervision.decode_jsonl`
and `validate_report_document` validate the field sets strictly and check
documented values: a status, stop reason, proof verdict, or aggregate outcome
outside its enum, a non-numeric duration, or a string where a Boolean or an
integer belongs is refused.

## Resource measurements and limits

On Linux, each sample sums the resident set size of every process in the owned
process group and reads host `MemAvailable` and `MemTotal` from
`/proc/meminfo`. The applicable cgroup v2 directory is resolved by joining the
`0::` membership path in `/proc/self/cgroup` to the cgroup2 superblock's mount
point and mount root in `/proc/self/mountinfo`; a host that mounts the
hierarchy at another point or with a non-root sub-tree is therefore resolved
correctly, and the fallback `/sys/fs/cgroup` is used only when mountinfo itself
is unreadable. Where readable, `memory.current`, `memory.peak`, and the `oom`,
`oom_kill`, and `max` counters in `memory.events` are recorded at harness start
and finish. Unreadable optional files produce an explicit absence and never
prevent a proof.

The measurements have deliberate limits:

- a peak is the largest periodic sample, so a spike between samples can be
  missed;
- summed RSS counts a shared page once per process that maps it, so it is an
  upper bound on unique resident memory;
- the samples bracket the whole invocation and therefore include compilation;
- process-group sampling covers only processes that stayed in the group the
  supervisor owns;
- cgroup values belong to the supervisor's cgroup and are normally shared with
  the rest of the job, so they are not exclusive per-harness measurements and
  their lifetime peaks are not harness peaks.

The report restates these limits in its `notes` array.

## Timeouts and watchdog

Each harness has a wall-clock budget. When it expires, the supervisor stops the
owned process group and classifies the harness as `timed-out`.

The host-memory watchdog compares each sample's `MemAvailable` with the
configured floor. When the floor is crossed, the supervisor stops the owned
group and classifies the harness as `memory-exhausted`. An unavailable
measurement never fires the watchdog, because a value that could not be read is
not evidence of pressure.

If both limits apply on one tick, the timeout is recorded, because it is
checked first. The first stop reason is never replaced.

## Signals and exit status

The library installs no signal handlers. The CLI installs SIGINT and SIGTERM
for the duration of one run and restores the previous handlers afterwards. On a
signal, the active harness is stopped, every unexecuted harness is reported as
`not-run`, the available report is finalized, and the CLI returns
`128 + signal` after printing the summary table. Standard output may be closed
early without replacing that status.

## Report schema, version 1

The final JSON report is written atomically when `--report PATH` or
`SupervisionConfig.sinks.report_path` is set. Its top-level keys are:

```json
{
  "schema": 1,
  "run_id": "…",
  "started_at": "2026-11-21T12:00:00.000Z",
  "finished_at": "2026-11-21T12:05:00.000Z",
  "outcome": "passed",
  "complete": true,
  "passed": true,
  "interrupted": false,
  "interrupt_signal": null,
  "expected": [{"package": "p", "harness": "h::x", "attempt": 1}],
  "accounting": {"missing": [], "duplicate": [], "unexpected": []},
  "results": [
    {
      "package": "p",
      "harness": "h::x",
      "attempt": 1,
      "executed": true,
      "status": "passed",
      "stop_reason": "exited",
      "exit_code": 0,
      "signal_number": null,
      "proof_verdict": "passed",
      "started_at": "…",
      "finished_at": "…",
      "duration_seconds": 12.5,
      "peak_group_rss": {"value": 123456},
      "min_host_available": {"value": 987654321},
      "host_total": {"value": 17179869184},
      "cgroup": null,
      "last_output": "…",
      "last_progress": "…",
      "diagnostic": {"sink": "jsonl", "offset": 4096},
      "detail": ""
    }
  ],
  "notes": ["…"]
}
```

`rust_prover_tools.supervision.validate_report_document` rejects an unknown
key, a missing key, an unknown schema version, or a malformed measurement.

## Python API

The public surface is available from `rust_prover_tools`:

```python
from pathlib import Path

from rust_prover_tools import (
    KaniHarnessRunOptions,
    LegacyRoute,
    SupervisionConfig,
    run_kani_harnesses,
)

run = run_kani_harnesses(
    KaniHarnessRunOptions(
        repo_root=Path.cwd(),
        route=LegacyRoute(
            installation=Path.home() / ".kani" / "kani-0.67.0",
            cwd=Path.cwd(),
        ),
        packages=("cuprum-native-io",),
        config=SupervisionConfig(timeout_seconds=1200.0),
    )
)

for result in run.report.results:
    print(result.spec.harness, result.status, result.peak_group_rss)

raise SystemExit(run.exit_status)
```

`run_kani_harnesses` (also exported as `run_harnesses`) loads or discovers the
inventory, selects exact harnesses, qualifies one route, runs the generic
supervisor, and returns a `KaniHarnessRun` with the typed `report`, the
`inventory`, the `selected` harnesses, the `backend_version`, and a `table`.
Async callers may use `run_supervised` with a `RunEnvironment` instead.

An application that owns its signal policy can split the two phases.
`prepare_harnesses` performs discovery, selection, and route qualification
without starting a prover or opening an evidence file. `execute_harnesses` runs
only that prepared selection, so handlers cover exactly the phase whose
children they can stop:

```python
prepared = prepare_harnesses(options)
with application_signal_policy():
    run = execute_harnesses(prepared, options.config)
```

`DiagnosticSinks` records the configured destinations without touching the
filesystem. `supervision.destinations.require_distinct_destinations` resolves
them and refuses two roles that name one file, so human and JSONL records can
never share one append handle. `kani_diagnostics.open_diagnostics` applies the
rule for library callers before opening a handle, and every CLI destination is
checked under the same rule before either file is opened.

Every dependency can be overridden through `KaniHarnessDependencies`: clock,
launcher, resource probe, cgroup reader, sleeper, diagnostics, interrupt
source, and route qualifier. This is how the deterministic tests run without a
live prover, real sleeps, or a real installation.

## Adapter contract

A prover adapter implements two methods:

```python
class HarnessAdapter(Protocol):
    def observer(self, spec: HarnessSpec) -> LineObserver: ...
    def classify(
        self, evidence: AttemptEvidence, observer: LineObserver
    ) -> HarnessClassification: ...
```

The observer receives every decoded line as it arrives and accumulates whatever
facts the classifier needs, bounded by the adapter. The classifier receives the
supervisor's termination evidence and returns a status, a proof verdict, a
bounded detail, and the last recognized progress line.

`KaniAdapter` is the reference implementation. It checks the registered grammar
before observation, feeds the shared 0.67.0 reader, and applies the precedence
in Table 2. The reader retains at most 256 refusal issues plus an exhaustion
sentinel, so a noisy harness cannot size the attempt's memory; a banner that
names an unregistered release is retained as a typed error rather than escaping
the line hook. An adapter for another prover must not reimplement timing,
measurement, evidence, or accounting.

The Kani adapter lives in the prover-neutral supervision package, as
`supervision/kani.py` and `supervision/kani_outcome.py`, because the adapter
seam is two methods. A second prover adds a sibling module rather than a new
package. Its observer (`supervision/kani_observer.py`, `KaniObserver` and
`KaniLiveFacts`) records live selection and progress facts and spools the
report for the parser. It never attributes a proof verdict: Kani escapes
nothing inside a check description, so description prose can contain a
verdict-shaped line, and only the parser has the lookahead that decides where
the description ended. A stopped attempt whose log did not parse therefore
reports `UNKNOWN`.

## Injected dependencies and their fallbacks

`RunEnvironment` and `KaniHarnessDependencies` are composition bags by design:
each field is one seam from `supervision/protocols.py`, and a consumer replaces
only the seam it needs. They deliberately mix process, time, inspection, and
evidence concerns because they are constructed once at the composition root and
passed through unchanged; splitting them would make every constructor call
repeat unrelated wiring.

Two convenience fallbacks sit at that boundary:

- `KaniHarnessDependencies.metadata` defaults to `cargo_metadata`, which runs
  `cargo metadata --no-deps --format-version 1` in the repository root. A
  caller that has already read the metadata, or a test, supplies another
  callable; preparation itself never calls Cargo directly.
- `LegacyRoute.environment` and `SnapshotRoute.environment` default to the
  process environment, read as `os.environ` when qualification runs. A
  `SnapshotRoute` whose `cwd` is `None` uses `Path.cwd()` for its proof working
  directory. Both defaults are read once at qualification, which is why the
  supervise path freezes the resulting environment with `EnvMode.REPLACE`. The
  CLI leaves them at their defaults; a library caller that needs an explicit
  environment passes it on the route.

## Test doubles

The deterministic suite uses:

- a virtual clock that advances only when a test asks;
- a virtual sleeper that advances that clock instead of waiting;
- a scripted launcher that emits selected lines, settles with a selected
  status, or waits to be stopped;
- a scripted resource probe with selected RSS and host-memory values;
- a scripted cgroup reader with selected paths and values;
- an in-memory JSONL sink and an interrupt source a test can fire.

One real-process witness proves that a stop reaches the owned group and that a
silent child still produces heartbeats. The live 0.67.0 fixture test is opt-in
through `RPT_LIVE_KANI=1`.

## Grammar qualification and qualify-once

Each grammar is qualified against one exact release and captured as a corpus of
a passing harness, a counterexample, a no-match run, and a truncated run.
`rust_prover_tools.kani_cli_grammar` is the single registry: `KANI_067` is the
qualified entry, `grammar_for` resolves a release, and an import-time check
requires the registry's `SUPPORTED_VERSIONS` to equal `SUPPORTED_KANI_VERSIONS`
from `kani_log`. Only 0.67.0 is registered. A banner naming another release
raises `UnsupportedKaniGrammarError`; the parser never guesses at a release it
was not written for. Adding a release requires all four captures, an entry in
the registry, and the version in `SUPPORTED_KANI_VERSIONS`.

A run qualifies its route once: installation and Cargo routing validation, one
child environment, and one rustc, compiler, and verifier identity probe
sequence. Every harness command is derived from that base. A run therefore
cannot observe one installation or environment while launching another, and a
concurrent snapshot repair cannot switch the active generation between
harnesses.

## Cuprum integration boundary

`rust_prover_tools.commands` remains the synchronous command boundary used by
installation and single-invocation routes. The supervised launcher is the async
boundary: it builds the same argv and environment, but uses `SafeCmd.run()` with
`capture=False`, `ProcessGroupPolicy.OWN_GROUP`, and a scoped `observe` hook
so it can sample the group and stop the child. `SafeCmd.start()` is not used
because its handle forces capture on, which would retain a whole proof log.

## Cuprum downstream work

Cuprum should consume this capability through thin configuration and workflow
wiring: its package-specific Make targets, CI matrix split, fault-check
dependencies, expected 19-harness inventory, and any proof repairs remain
downstream work in [cuprum#723](https://github.com/leynos/cuprum/issues/723).
This library does not encode Cuprum's inventory, repository layout, or output
directory, and it adds no unwind override, unwinding-assertion change, or
proof-domain narrowing.

## References

- [Users' guide](users-guide.md) for every command and option.
- [Developers' guide](developers-guide.md) for module responsibilities and
  gates.
- [CLI design](prover-tools-cli-design.md) for the command architecture.
- [Combobulate](https://github.com/leynos/combobulate) at commit `9dd6e3c` for
  the shared Kani 0.67.0 grammar, used under the ISC licence recorded in
  `rust_prover_tools/kani_log/ATTRIBUTION.md`.
