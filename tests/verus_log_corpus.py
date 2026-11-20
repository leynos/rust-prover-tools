"""Read the committed Verus log corpus and the facts it was captured from.

The corpus is the oracle: every committed log is real Verus output captured on
a named release, and ``expected.json`` records what the parser and the decision
must make of it.  The corpus is version-indexed, so a supported release's logs
live under ``tests/fixtures/verus_logs/<version>``; only the first supported
version is read here, because the corpus-wide checks state facts about the
release under test.

Each log is real output rather than a hand-written sample, which is what makes
the grammar trustworthy: a change to :mod:`rust_prover_tools.verus_log.syntax`
that stops matching a real release shows up as a corpus failure.
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.verus_log import (
    SUPPORTED_VERUS_VERSIONS,
    VerusAnalysis,
    VerusLog,
    VerusLogError,
    analyse_verus_log,
    from_verus_log_error,
    parse_verus_log,
)

__all__ = (
    "CORPUS",
    "CORPUS_ROOT",
    "CORPUS_VERSION",
    "analysis_for",
    "expected",
    "log_names",
    "log_text",
    "parse_log",
    "recorded",
    "z3_banner",
)

ROOT = Path(__file__).resolve().parents[1]

#: Corpus root holding one directory per qualified Verus release.
CORPUS_ROOT = ROOT / "tests" / "fixtures" / "verus_logs"

#: The release whose corpus the corpus-wide checks read.
CORPUS_VERSION = SUPPORTED_VERUS_VERSIONS[0]
CORPUS = CORPUS_ROOT / CORPUS_VERSION


def log_names() -> tuple[str, ...]:
    """Name every committed log, in a stable order, without its extension."""
    return tuple(sorted(path.stem for path in CORPUS.glob("*.log")))


def log_text(name: str) -> str:
    """Return one committed log exactly as the parser receives it."""
    return CORPUS.joinpath(f"{name}.log").read_text(encoding="utf-8")


def expected() -> dict[str, typ.Any]:
    """Return the committed record of what every log must yield and decide."""
    return typ.cast(
        "dict[str, typ.Any]",
        json.loads(CORPUS.joinpath("expected.json").read_text(encoding="utf-8")),
    )


def z3_banner() -> str:
    """Return the solver banner the corpus release was run against."""
    return typ.cast("str", expected()["z3"])


def parse_log(name: str) -> VerusLog:
    """Parse one committed log, raising the structural error it may have."""
    return parse_verus_log(log_text(name))


def analysis_for(name: str) -> VerusAnalysis:
    """Decide one committed log, completing a structural error as a tool error."""
    try:
        log = parse_log(name)
    except VerusLogError as error:
        return from_verus_log_error(error)
    return analyse_verus_log(log)


def recorded(name: str) -> dict[str, typ.Any]:
    """Return one log's recorded expectation."""
    return typ.cast("dict[str, typ.Any]", expected()["logs"][name])
