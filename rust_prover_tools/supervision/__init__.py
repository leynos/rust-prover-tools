"""Supervise prover harnesses with live evidence and attributable outcomes.

The package divides one job into four parts:

- ``models`` holds the typed configuration-independent values: identity,
  measurements, samples, termination evidence, and the run report.
- ``config`` validates the per-harness policy and the diagnostic destinations.
- ``attempt`` runs one invocation, streams its output, samples its process
  group, applies the deadline and the host-memory watchdog, and returns the
  termination evidence.
- ``runner`` sequences attempts, continues past individual failures, compares
  expected and executed identities as multisets, and finalizes the report.

``launcher`` is the Cuprum boundary, ``resources`` reads Linux procfs and
cgroup v2, ``sinks`` and ``events`` write the evidence, and ``report`` renders
the final table and JSON. ``accounting`` supplies the multiset comparison that
both inventory pin drift and run completeness use.

The package is prover-neutral. A prover adapter supplies a command builder and
a classifier; it does not reimplement timing, measurement, evidence, or
accounting.

Example
-------
```python
report = run_supervised_sync(
    (HarnessSpec("example", "harnesses::verified"),),
    SupervisionConfig(),
    environment,
)
print(report.passed)
```
"""

from __future__ import annotations

from rust_prover_tools.supervision.accounting import (
    compare_identities,
    identity_key,
)
from rust_prover_tools.supervision.attempt import AttemptSupervisor
from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence
from rust_prover_tools.supervision.clock import AsyncSleeper, SystemClock
from rust_prover_tools.supervision.config import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WATCHDOG_THRESHOLD_BYTES,
    DiagnosticSinks,
    SupervisionConfig,
)
from rust_prover_tools.supervision.destinations import (
    require_distinct_destinations,
)
from rust_prover_tools.supervision.formatting import (
    JSONL_SCHEMA_VERSION,
    MIN_LINE_BYTES,
    TRUNCATION_MARKER,
    bounded_text,
    fit_record,
    utc_text,
)
from rust_prover_tools.supervision.human import render_human
from rust_prover_tools.supervision.launcher import (
    CommandBuilder,
    CuprumHarnessLauncher,
    LaunchCommand,
    LaunchEnvironment,
    RunningHarness,
)
from rust_prover_tools.supervision.memory_sink import MemorySink
from rust_prover_tools.supervision.models import (
    CgroupMemory,
    CgroupObservation,
    ExecutionStatus,
    HarnessSpec,
    LaunchResult,
    Measurement,
    OutputLine,
    ProofVerdict,
    ResourceSample,
    StopReason,
)
from rust_prover_tools.supervision.protocols import (
    HarnessAdapter,
    LineObserver,
)
from rust_prover_tools.supervision.report import (
    REPORT_NOTES,
    REPORT_SCHEMA_VERSION,
    render_table,
    report_document,
)
from rust_prover_tools.supervision.report_writer import write_report
from rust_prover_tools.supervision.resources import (
    CgroupV2Reader,
    ProcfsResourceProbe,
    ensure_linux,
    read_meminfo,
    sum_group_rss,
)
from rust_prover_tools.supervision.results import (
    DiagnosticRef,
    HarnessClassification,
    HarnessResult,
    InventoryDrift,
    RunOutcome,
    RunReport,
)
from rust_prover_tools.supervision.run_environment import RunEnvironment
from rust_prover_tools.supervision.runner import (
    SupervisedRun,
    new_run_id,
    run_supervised,
    run_supervised_sync,
)
from rust_prover_tools.supervision.schema import (
    JSONL_FIELDS,
    REPORT_FIELDS,
    EvidenceFormatError,
    decode_jsonl,
    decode_measurement,
    validate_report_document,
)
from rust_prover_tools.supervision.sinks import (
    Diagnostics,
    HumanSink,
    JsonlSink,
    NullSink,
    jsonl_append_offset,
)
from rust_prover_tools.supervision.watchdog import (
    SampleVerdict,
    deadline_verdict,
    watchdog_verdict,
)

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WATCHDOG_THRESHOLD_BYTES",
    "JSONL_FIELDS",
    "JSONL_SCHEMA_VERSION",
    "MIN_LINE_BYTES",
    "REPORT_FIELDS",
    "REPORT_NOTES",
    "REPORT_SCHEMA_VERSION",
    "TRUNCATION_MARKER",
    "AsyncSleeper",
    "AttemptEvidence",
    "AttemptSupervisor",
    "CgroupMemory",
    "CgroupObservation",
    "CgroupV2Reader",
    "CommandBuilder",
    "CuprumHarnessLauncher",
    "DiagnosticRef",
    "DiagnosticSinks",
    "Diagnostics",
    "EvidenceFormatError",
    "ExecutionStatus",
    "HarnessAdapter",
    "HarnessClassification",
    "HarnessResult",
    "HarnessSpec",
    "HumanSink",
    "InventoryDrift",
    "JsonlSink",
    "LaunchCommand",
    "LaunchEnvironment",
    "LaunchResult",
    "LineObserver",
    "Measurement",
    "MemorySink",
    "NullSink",
    "OutputLine",
    "ProcfsResourceProbe",
    "ProofVerdict",
    "ResourceSample",
    "RunEnvironment",
    "RunOutcome",
    "RunReport",
    "RunningHarness",
    "SampleVerdict",
    "StopReason",
    "SupervisedRun",
    "SupervisionConfig",
    "SystemClock",
    "bounded_text",
    "compare_identities",
    "deadline_verdict",
    "decode_jsonl",
    "decode_measurement",
    "ensure_linux",
    "fit_record",
    "identity_key",
    "jsonl_append_offset",
    "new_run_id",
    "read_meminfo",
    "render_human",
    "render_table",
    "report_document",
    "require_distinct_destinations",
    "run_supervised",
    "run_supervised_sync",
    "sum_group_rss",
    "utc_text",
    "validate_report_document",
    "watchdog_verdict",
    "write_report",
]
