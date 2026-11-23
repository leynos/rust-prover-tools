"""Read a Kani verbose log into per-harness facts.

The package is split by what each part needs to know:

- :mod:`.syntax` names the grammar: the regular expressions, markers and
  statuses a Kani log is read with.
- :mod:`.model` holds the frozen value types a parsed log produces.
- :mod:`.reader` consumes one report construct at a time and refuses any line
  inside a report that the grammar does not account for.
- :mod:`.reports` consumes the report sections inside a harness block.
- :mod:`.extract` recognises the log's shape - banner, harness blocks, manual
  summary - and reconciles their counts.
- :mod:`.manifest` holds the claims, controls and expected failures a decision
  is taken against.
- :mod:`.decide` maps a parsed log and its manifest to one outcome.

The grammar is pinned to :data:`SUPPORTED_KANI_VERSIONS`.  A log from another
release is refused rather than guessed at; adding a release is a deliberate act
that starts with a captured corpus.

Examples
--------
>>> parse_kani_log("not a log")
Traceback (most recent call last):
KaniLogError: structural error: the log does not begin with a Kani banner
"""

from __future__ import annotations

from rust_prover_tools.kani_log.decide import (
    ACCEPTED,
    FOCUSED,
    FULL_PROFILE,
    NOT_RUN,
    REJECTED_AS_INTENDED,
    REJECTED_OTHERWISE,
    SATISFIED,
    UNSATISFIED,
    KaniAnalysis,
    KaniOutcome,
    analyse_kani_log,
    from_kani_log_error,
)
from rust_prover_tools.kani_log.extract import (
    parse_kani_log,
    unwinding_checks_enabled,
)
from rust_prover_tools.kani_log.manifest import (
    CLAIM,
    CONTROL,
    MANIFEST_SCHEMA_VERSION,
    Harness,
    KaniManifestError,
    Manifest,
    manifest_from_document,
    parse_manifest,
)
from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog, ManualSummary
from rust_prover_tools.kani_log.syntax import (
    ASSERTION_KIND,
    BANNER,
    COMPLETE,
    HARNESS_START,
    LIST_WRITTEN,
    NO_HARNESS_MATCH,
    NOISE_PREFIXES,
    SUPPORTED_KANI_VERSIONS,
    VERIFICATION,
    FailedCheck,
    KaniLogError,
    PropertyResult,
)

__all__ = (
    "ACCEPTED",
    "ASSERTION_KIND",
    "BANNER",
    "CLAIM",
    "COMPLETE",
    "CONTROL",
    "FOCUSED",
    "FULL_PROFILE",
    "HARNESS_START",
    "LIST_WRITTEN",
    "MANIFEST_SCHEMA_VERSION",
    "NOISE_PREFIXES",
    "NOT_RUN",
    "NO_HARNESS_MATCH",
    "REJECTED_AS_INTENDED",
    "REJECTED_OTHERWISE",
    "SATISFIED",
    "SUPPORTED_KANI_VERSIONS",
    "UNSATISFIED",
    "VERIFICATION",
    "FailedCheck",
    "Harness",
    "HarnessBlock",
    "KaniAnalysis",
    "KaniLog",
    "KaniLogError",
    "KaniManifestError",
    "KaniOutcome",
    "Manifest",
    "ManualSummary",
    "PropertyResult",
    "analyse_kani_log",
    "from_kani_log_error",
    "manifest_from_document",
    "parse_kani_log",
    "parse_manifest",
    "unwinding_checks_enabled",
)
