"""Read the committed Kani log corpus and the manifests it was captured against.

The corpus is the oracle: every committed log is real Kani output, and
``expected.json`` records what the parser and the decision must make of it.  The
helpers here read those files; :mod:`tests.kani_log_fixtures` builds synthetic
fact combinations for the exhaustive checks.

The corpus is version-indexed, so a supported release's logs live under
``tests/fixtures/kani_logs/<version>``.  Only the first supported version is
read here: the corpus-wide checks state facts about the release under test.
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.kani_log import (
    SUPPORTED_KANI_VERSIONS,
    KaniAnalysis,
    KaniLog,
    Manifest,
    analyse_kani_log,
)

__all__ = (
    "CORPUS",
    "CORPUS_ROOT",
    "CORPUS_SELECTIONS",
    "CORPUS_VERSION",
    "TABLE",
    "corpus_decision_for",
    "corpus_manifest",
    "expected",
    "in_place_manifest",
    "log_names",
    "log_text",
    "manifest_for",
    "parse_log",
    "recorded",
    "table",
)

ROOT = Path(__file__).resolve().parents[1]

#: Corpus root holding one directory per qualified Kani release.
CORPUS_ROOT = ROOT / "tests" / "fixtures" / "kani_logs"

#: The release whose corpus the corpus-wide checks read.
CORPUS_VERSION = SUPPORTED_KANI_VERSIONS[0]
CORPUS = CORPUS_ROOT / CORPUS_VERSION
TABLE = CORPUS / "decision_table.json"

#: The harnesses each committed log was captured for.
CORPUS_SELECTIONS = {
    "verified": ("harnesses::verified",),
    "counterexample": ("harnesses::counterexample",),
    "unsatisfied_cover": ("harnesses::unsatisfied_cover",),
    "multiplication_timeout": ("harnesses::multiplication_timeout",),
    "insufficient_unwind": ("harnesses::insufficient_unwind",),
    "vacuous_assumption": ("harnesses::vacuous_assumption",),
    "unsupported": ("harnesses::unsupported",),
    "expected_panic": ("harnesses::expected_panic",),
    "seeded_wrapping_mutant": ("harnesses::seeded_wrapping_mutant",),
    "selected_harness": ("harnesses::verified",),
    "fail_fast": (
        "harnesses::counterexample",
        "harnesses::verified",
        "harnesses::seeded_wrapping_mutant",
    ),
    "default_unwind": (
        "harnesses::verified",
        "harnesses::seeded_wrapping_mutant",
    ),
}


def log_names() -> tuple[str, ...]:
    """Name every committed log, in a stable order, without its extension."""
    return tuple(sorted(path.stem for path in CORPUS.glob("*.log")))


def log_text(name: str) -> str:
    """Return one committed log exactly as the parser receives it."""
    return CORPUS.joinpath(f"{name}.log").read_text(encoding="utf-8")


def parse_log(name: str) -> KaniLog:
    """Parse one committed log, raising the structural error it may have."""
    from rust_prover_tools.kani_log import parse_kani_log

    return parse_kani_log(log_text(name))


def expected() -> dict[str, typ.Any]:
    """Return the committed record of what every log must yield and decide."""
    return typ.cast(
        "dict[str, typ.Any]",
        json.loads(CORPUS.joinpath("expected.json").read_text(encoding="utf-8")),
    )


def table() -> dict[str, typ.Any]:
    """Return the decision precedence table as data."""
    return typ.cast(
        "dict[str, typ.Any]",
        json.loads(TABLE.read_text(encoding="utf-8")),
    )


def _manifest_file(name: str) -> Manifest:
    """Read one committed manifest document."""
    from rust_prover_tools.kani_log import parse_manifest

    return parse_manifest(
        CORPUS.joinpath(name).read_text(encoding="utf-8"),
    )


def corpus_manifest() -> Manifest:
    """Return the manifest for every harness in the corpus crate."""
    return _manifest_file("corpus-manifest.json")


def in_place_manifest() -> Manifest:
    """Return the standalone manifest for the probe-generated harness."""
    return _manifest_file("in_place-manifest.json")


def manifest_for(name: str) -> Manifest:
    """Return the manifest one log must be decided against, narrowed to its run."""
    if name == "in_place":
        return in_place_manifest()
    selected = CORPUS_SELECTIONS.get(name)
    if not selected:
        return corpus_manifest()
    return corpus_manifest().select(selected)


def corpus_decision_for(name: str) -> KaniAnalysis:
    """Decide one committed log against the manifest its command implies."""
    from rust_prover_tools.kani_log import KaniLogError, from_kani_log_error

    try:
        log = parse_log(name)
    except KaniLogError as error:
        return from_kani_log_error(error)
    return analyse_kani_log(log, manifest_for(name))


def recorded(name: str) -> tuple[object, ...]:
    """Return one log's recorded decision in the order this package reports it."""
    entry = expected()["decisions"][name]
    controls = tuple(str(observed) for _required, observed in entry["controls"])
    return (
        entry["outcome"],
        tuple(entry["witnesses"]),
        controls,
        entry["run_mode"],
    )
