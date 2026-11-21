"""Read a captured Verus run and decide what it proves.

Verus writes one `verification results::` summary plus a diagnostic for every
function it could not prove.  The package turns that text into facts and then
into a decision:

- :mod:`.syntax` names the grammar: the summary, the diagnostic header, the
  span line and the selection note, with the release the corpus came from.
- :mod:`.model` holds the frozen values a parsed log produces.
- :mod:`.reader` scans the log once, in either stream order, and refuses a log
  that contradicts itself.
- :mod:`.expectation` declares where a deliberately broken proof must fail, so
  a negative control cannot pass by failing somewhere else.
- :mod:`.decide` maps a parsed log, and any declared expectation, to an
  outcome and a run mode.
- :mod:`.checks` holds a run to the counts, failure region and exit status a
  qualification caller declared, which is what a seeded-mutant run needs: it is
  *meant* to fail, so its outcome cannot be the pass criterion.

The grammar is qualified against :data:`SUPPORTED_VERUS_VERSIONS`; the corpus
under ``tests/fixtures/verus_logs`` records the runs it was read from.

Examples
--------
>>> log = parse_verus_log("verification results:: 2 verified, 0 errors")
>>> analyse_verus_log(log).passed
True
"""

from __future__ import annotations

from rust_prover_tools.verus_log.checks import (
    LogExpectations,
    LogVerdict,
    check_verus_log,
)
from rust_prover_tools.verus_log.decide import (
    FULL_RUN,
    PARTIAL_RUN,
    VerusAnalysis,
    VerusOutcome,
    analyse_verus_log,
    from_verus_log_error,
)
from rust_prover_tools.verus_log.expectation import (
    FailureExpectation,
    parse_expectation,
)
from rust_prover_tools.verus_log.model import VerusFailure, VerusLog, VerusSummary
from rust_prover_tools.verus_log.reader import parse_verus_log
from rust_prover_tools.verus_log.syntax import (
    SUPPORTED_VERUS_VERSIONS,
    VerusLogError,
)

__all__ = (
    "FULL_RUN",
    "PARTIAL_RUN",
    "SUPPORTED_VERUS_VERSIONS",
    "FailureExpectation",
    "LogExpectations",
    "LogVerdict",
    "VerusAnalysis",
    "VerusFailure",
    "VerusLog",
    "VerusLogError",
    "VerusOutcome",
    "VerusSummary",
    "analyse_verus_log",
    "check_verus_log",
    "from_verus_log_error",
    "parse_expectation",
    "parse_verus_log",
)
