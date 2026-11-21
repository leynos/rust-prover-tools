"""Read a Kani 0.67.0 log into per-harness facts.

The package is split by what each part needs to know:

- ``syntax`` names the grammar: the regular expressions, markers, and
  statuses a Kani log is read with.
- ``model`` holds the parsed value types, so a test can construct facts
  without a log and the walker stays the only module that needs the grammar.
- ``blocks`` accumulates one harness block and reconciles its counts.
- ``reader`` consumes one report construct at a time, recording a typed reason
  wherever the grammar cannot account for a line.
- ``summary`` reconciles a complete report's blocks against its manual summary.
- ``registry`` registers the exact releases the grammar was qualified against,
  the output arguments the library adds, the selector and output spellings a
  caller may not supply, and the pretty-name derivation.
- ``extract`` is the whole-text convenience over the same incremental reader.

The grammar, model, reader logic, and test corpus derive from Combobulate at
commit ``9dd6e3c``; see ``ATTRIBUTION.md``.

Examples
--------
>>> from rust_prover_tools.kani_log import parse, KaniLogError
>>> try:
...     parse("not a log")
... except KaniLogError as error:
...     print(error)
structural error: the log does not begin with a Kani banner
"""

from __future__ import annotations

from rust_prover_tools.kani_log.budget import MAX_RETAINED_ISSUES
from rust_prover_tools.kani_log.extract import (
    parse,
    parse_lines,
    unwinding_checks_enabled,
)
from rust_prover_tools.kani_log.model import (
    HarnessBlock,
    KaniLog,
    KaniLogFacts,
    ManualSummary,
)
from rust_prover_tools.kani_log.reader import KaniLogReader
from rust_prover_tools.kani_log.registry import (
    KANI_067,
    SUPPORTED_VERSIONS,
    ForbiddenOption,
    KaniGrammar,
    grammar_for,
    pretty_name,
    supported_versions,
)
from rust_prover_tools.kani_log.syntax import (
    BANNER,
    FailedCheck,
    KaniLogError,
    PropertyResult,
    UnsupportedKaniGrammarError,
)

__all__ = [
    "BANNER",
    "KANI_067",
    "MAX_RETAINED_ISSUES",
    "SUPPORTED_VERSIONS",
    "FailedCheck",
    "ForbiddenOption",
    "HarnessBlock",
    "KaniGrammar",
    "KaniLog",
    "KaniLogError",
    "KaniLogFacts",
    "KaniLogReader",
    "ManualSummary",
    "PropertyResult",
    "UnsupportedKaniGrammarError",
    "grammar_for",
    "parse",
    "parse_lines",
    "pretty_name",
    "supported_versions",
    "unwinding_checks_enabled",
]
