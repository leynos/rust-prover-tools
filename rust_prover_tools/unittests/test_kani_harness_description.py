"""Hold description framing so quoted content cannot become evidence."""

from __future__ import annotations

from rust_prover_tools.supervision import (
    ExecutionStatus,
    OutputLine,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.kani import (
    KaniAdapter,
    KaniObserver,
)
from rust_prover_tools.unittests.kani_harness_classifier_support import (
    _evidence,
    _finish,
    _log,
    _log_with_selection_noise,
    _observed_log,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock


def test_a_selection_shaped_description_line_is_not_a_selection() -> None:
    """Description content cannot add a selection record.

    Kani prints descriptions without escaping newlines, so a proof message can
    contain a line that looks exactly like a selection record. The parsed log
    knows the frame; the live scanner does not. Classification must trust the
    parse, or a passing single-harness run fails as ``multiple-match``.
    """
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Checking harness fake...",
        'last"',
    ]
    observer = KaniObserver()
    clock = VirtualClock()
    for line in log:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    assert "fake" not in observer.facts.selection_records
    adapter = KaniAdapter()

    classification = adapter.classify(
        _evidence("harnesses::verified"), _finish(observer)
    )

    assert classification.status is ExecutionStatus.PASSED
    assert classification.observed == ("alpha::harnesses::verified",)


def test_description_noise_cannot_exhaust_the_live_facts() -> None:
    """Description content is excluded, so it cannot spend the record budget.

    A description quoting more selection-shaped lines than the budget holds
    leaves the live facts intact, and the parsed report still passes.
    """
    from rust_prover_tools.supervision.kani_observer import MAX_LIVE_RECORDS

    observer = _observed_log(_log_with_selection_noise(MAX_LIVE_RECORDS + 10))
    assert not observer.facts_truncated
    adapter = KaniAdapter()

    classification = adapter.classify(
        _evidence("harnesses::verified"), _finish(observer)
    )

    assert classification.status is ExecutionStatus.PASSED
    assert classification.observed == ("alpha::harnesses::verified",)


def test_a_description_verdict_does_not_become_a_live_verdict() -> None:
    """A quoted verdict line is description content, not a result.

    The attempt is interrupted before its genuine verdict, so a live verdict
    recorded from the description would be the only one and would publish
    `PASSED` for a proof the verifier never reported.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    verdict = next(
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    )
    observer = _observed_log(log[:verdict])
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_description_banner_does_not_replace_the_opening_release() -> None:
    """Only the log's opening banner names the release."""
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Kani Rust Verifier 0.68.0 (cargo plugin)",
        'last"',
    ]
    observer = _observed_log(log)
    assert observer.banner_version == "0.67.0"

    classification = KaniAdapter().classify(
        _evidence("harnesses::verified"), _finish(observer)
    )

    assert classification.status is ExecutionStatus.PASSED


def test_a_parsed_mismatch_does_not_keep_a_live_verdict() -> None:
    """The parse is authoritative for the verdict as well as the identities.

    A description can contain a selection-shaped line and a verdict for the
    requested name while the verifier actually ran another harness. The
    parsed log names only `other`, so the mismatch must report no attributed
    verdict.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "Checking harness harnesses::requested...",
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    observer = _observed_log(log)

    classification = KaniAdapter().classify(
        _evidence("harnesses::requested", exit_code=1), _finish(observer)
    )

    assert classification.status is ExecutionStatus.NO_MATCH
    assert classification.proof_verdict is ProofVerdict.UNKNOWN
    assert classification.observed == ("alpha::harnesses::other",)


def test_a_description_ending_in_a_quote_still_hides_its_verdict() -> None:
    """An embedded quote is not evidence that the description ended.

    Kani escapes nothing, so a description can contain a line ending in a
    quotation mark followed by a verdict-shaped line. The attempt stops before
    its genuine verdict, and the live verdict must stay unknown.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first embedded"',
        "VERIFICATION:- SUCCESSFUL",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    assert len(verdicts) == 2
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_an_ordinary_continuation_does_not_commit_a_description_verdict() -> None:
    """Description content followed by ordinary text stays description content.

    Kani escapes nothing, so a quoted verdict line followed by plain text and
    then `last"` is still one description. The frame closes at the real
    `Location:` field, so nothing from the description is credited.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first embedded"',
        "VERIFICATION:- SUCCESSFUL",
        "ordinary continuation",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_a_stopped_attempt_does_not_claim_a_verdict_from_prose() -> None:
    """No live verdict is attributed, so description prose cannot fabricate one.

    A description quoting `RESULTS:` and a verdict is ambiguous to any
    line-level scan. A stopped attempt therefore reports `UNKNOWN` unless its
    log parsed.
    """
    log = _log("verified", "harnesses::other")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "RESULTS:",
        "VERIFICATION:- SUCCESSFUL",
        "ordinary continuation",
        'last"',
    ]
    verdicts = [
        index for index, line in enumerate(log) if line.startswith("VERIFICATION:-")
    ]
    observer = _observed_log(log[: verdicts[1]])
    assert observer.facts.verdicts == ()
    evidence = _evidence(
        "harnesses::other",
        stop=StopReason.TIMEOUT,
        exit_code=None,
        detail="timed out",
    )

    classification = KaniAdapter().classify(evidence, _finish(observer))

    assert classification.proof_verdict is ProofVerdict.UNKNOWN


def test_results_inside_a_description_cannot_credit_a_fake_selection() -> None:
    """A results marker is description content and cannot close the frame.

    Kani escapes nothing inside a description, so ``RESULTS:`` followed by a
    selection-shaped line can be prose. A stopped log never reaches the parse,
    which makes the live facts decisive: the fake name must not appear at all,
    and the real selection recorded before the description must survive.
    """
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "RESULTS:",
        "Checking harness fake...",
        'last"',
    ]
    observer = KaniObserver()
    clock = VirtualClock()
    for line in log:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=line,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )

    assert "fake" not in observer.facts.selection_records
    assert "harnesses::verified" in observer.facts.selection_records


def test_a_stopped_run_does_not_credit_a_fake_selection_after_results() -> None:
    """A stopped attempt credits only the real selection, not quoted prose."""
    log = _log("verified", "harnesses::verified")
    description = next(
        index for index, line in enumerate(log) if "Description:" in line
    )
    log[description : description + 1] = [
        '\t - Description: "first',
        "RESULTS:",
        "Checking harness fake...",
        'last"',
    ]
    observer = _observed_log(log)
    adapter = KaniAdapter()

    classification = adapter.classify(
        _evidence("harnesses::verified", stop=StopReason.TIMEOUT),
        _finish(observer),
    )

    assert classification.status is ExecutionStatus.TIMED_OUT
    assert classification.observed == ("alpha::harnesses::verified",)
