"""Shared corpus fixtures and projections for the Kani parser tests."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_log import (
    SUPPORTED_KANI_VERSIONS,
    HarnessBlock,
    KaniLog,
    KaniLogError,
    parse_kani_log,
)

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs"
)
#: Each qualified release keeps its own corpus directory, named by version.
CORPORA: typ.Final[tuple[Path, ...]] = tuple(
    FIXTURES / version for version in SUPPORTED_KANI_VERSIONS
)
CURRENT_CORPUS: typ.Final[Path] = CORPORA[0]


def corpus_expected(corpus: Path) -> dict[str, typ.Any]:
    """Return one corpus directory's recorded facts and decisions."""
    return typ.cast(
        "dict[str, typ.Any]",
        json.loads((corpus / "expected.json").read_text(encoding="utf-8")),
    )


EXPECTED: typ.Final[dict[str, dict[str, typ.Any]]] = {
    corpus.name: corpus_expected(corpus) for corpus in CORPORA
}
CORPUS_CASES: typ.Final[tuple[tuple[str, str], ...]] = tuple(
    (version, name)
    for version, entries in EXPECTED.items()
    for name in sorted(entries["logs"])
)
LOG_NAMES: typ.Final[tuple[str, ...]] = tuple(
    sorted(EXPECTED[CURRENT_CORPUS.name]["logs"])
)
CHATTER_LINE = "chatter inserted between reports"


def log_text(name: str, version: str | None = None) -> str:
    """Return one committed log exactly as the parser receives it.

    ``version`` selects a corpus directory; it defaults to the current
    qualified release so the single-release tests stay readable.
    """
    corpus = FIXTURES / (version or CURRENT_CORPUS.name)
    return (corpus / f"{name}.log").read_text(encoding="utf-8")


def block_facts(block: HarnessBlock) -> dict[str, object]:
    """Reduce one block to the facts the corpus records."""
    return {
        "name": block.name,
        "verdict": block.verdict,
        "failed": block.failed_count,
        "total": block.total_count,
        "other": [list(pair) for pair in block.other_counts],
        "timed_out": block.timed_out,
        "unwinding_reported": block.unwinding_reported,
        "panics_as_expected": block.panics_as_expected,
        "checks": [[item.kind, item.status] for item in block.properties],
        "covers": [[item.description, item.status] for item in block.covers],
        "failures": [[item.message, item.path, item.line] for item in block.failures],
    }


def refused(text: str) -> str:
    """Return the structural reason a log is refused.

    Parameters
    ----------
    text : str
        Log text expected to be refused.

    Returns
    -------
    str
        The parser's reason without its ``structural error:`` prefix.

    Raises
    ------
    AssertionError
        If the parser accepts the text.

    """
    with pytest.raises(KaniLogError) as caught:
        parse_kani_log(text)
    return caught.value.reason


def parse_text(text: str) -> KaniLog:
    """Parse a log that the test expects to be accepted."""
    return parse_kani_log(text)
