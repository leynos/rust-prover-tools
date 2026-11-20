"""Prove the suite would notice a plausible weaker analysis.

A passing suite says nothing about its own sensitivity.  Each broken analysis
below is a reading a hurried implementation could plausibly ship - trust the
printed verdict, ignore the manifest, always report success - and the suite
must name at least one committed log where that reading disagrees with the
recorded decision.  The real analysis is measured first, so a disagreement is
the broken analysis's and not the record's.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log import (
    CLAIM,
    Harness,
    KaniAnalysis,
    KaniLog,
    KaniLogError,
    KaniOutcome,
    Manifest,
    analyse_kani_log,
    from_kani_log_error,
)
from tests import kani_log_corpus as corpus
from tests import kani_log_properties as props

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def broken_always_verified(
    _log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis that ignores the log entirely."""
    return KaniAnalysis(KaniOutcome.VERIFIED)


def broken_success_line_only(
    log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis that trusts Kani's printed verdict and nothing else."""
    blocks = log.blocks
    outcome = (
        KaniOutcome.VERIFIED
        if all(block.succeeded for block in blocks)
        else KaniOutcome.COUNTEREXAMPLE
    )
    return KaniAnalysis(outcome)


def broken_manifest_ignored(
    log: KaniLog,
    _manifest: Manifest,
) -> KaniAnalysis:
    """Return an analysis taken against a manifest invented from the log itself."""
    harnesses = log.harnesses
    if not harnesses:
        return broken_always_verified(log, _manifest)
    invented = Manifest(
        tuple(Harness(name, CLAIM) for name in harnesses),
    )
    return analyse_kani_log(log, invented)


def _apply(
    decide_function: cabc.Callable[[KaniLog, Manifest], KaniAnalysis],
    name: str,
) -> KaniAnalysis:
    """Run one broken analysis over one corpus log, refusing nothing it cannot parse."""
    try:
        log = corpus.parse_log(name)
    except KaniLogError as error:
        return from_kani_log_error(error)
    return decide_function(log, corpus.manifest_for(name))


def test_the_real_decision_gets_nothing_wrong() -> None:
    """Establish the reference the broken analyses are measured against."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(corpus.corpus_decision_for(name)) != corpus.recorded(name)
    ]
    assert wrong == []


def test_always_verified_is_wrong() -> None:
    """Reject an analysis that ignores the log and reports success."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_always_verified, name))
        != corpus.recorded(name)
    ]
    assert wrong


def test_the_success_line_alone_is_wrong() -> None:
    """Reject an analysis that trusts the printed verdict and ignores covers."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_success_line_only, name))
        != corpus.recorded(name)
    ]
    assert "vacuous_assumption" in wrong


def test_ignoring_the_manifest_is_wrong() -> None:
    """Reject an analysis that never reads roles, witnesses or expected failures."""
    wrong = [
        name
        for name in corpus.log_names()
        if props.summarise(_apply(broken_manifest_ignored, name))
        != corpus.recorded(name)
    ]
    assert "seeded_wrapping_mutant" in wrong
