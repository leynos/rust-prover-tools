"""Tests for strict readings of Kani proof transcripts.

A process exit status cannot tell a failed assertion from a compile error, a
solver crash or a run that discovered no harnesses. The validator therefore
reads the named harness starts, their individual verdicts and the manual
summary, and refuses any transcript that does not state one consistent result
for the exact harness set the smoke expects.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_snapshot.producer.proof_report import (
    MAX_TRANSCRIPT_BYTES,
    HarnessOutcome,
    ProofCompileError,
    ProofSolverError,
    ProofTranscriptError,
    parse_proof_report,
    render_proof_report,
    require_named_harnesses,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

FIXTURES: typ.Final[pathlib.Path] = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_snapshot"
    / "transcripts"
)

_NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True)


def _fixture(name: str) -> str:
    """Return one captured transcript as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def _block(name: str, *, verified: bool) -> str:
    """Render one harness block in the shape the pinned Kani writes."""
    if verified:
        result = " ** 0 of 4 failed\n\nVERIFICATION:- SUCCESSFUL"
    else:
        result = " ** 1 of 4 failed\nFailed Checks: expected\n\nVERIFICATION:- FAILED"
    return (
        f"Checking harness {name}...\n\nSUMMARY:\n{result}\nVerification Time: 0.1s\n"
    )


def _transcript(verified: cabc.Iterable[str], failed: cabc.Iterable[str]) -> str:
    """Build one transcript in the shape the pinned Kani writes."""
    verified_names = list(verified)
    failed_names = list(failed)
    blocks = [_block(name, verified=True) for name in verified_names]
    blocks.extend(_block(name, verified=False) for name in failed_names)
    summary = [
        "\nManual Harness Summary:\n",
        *(f"Verification failed for - {name}\n" for name in failed_names),
        (
            f"Complete - {len(verified_names)} successfully verified harnesses, "
            f"{len(failed_names)} failures, "
            f"{len(verified_names) + len(failed_names)} total.\n"
        ),
    ]
    return (
        "Kani Rust Verifier 0.68.0 (standalone)\n"
        + "\n".join(blocks)
        + "".join(summary)
    )


def test_the_positive_transcript_reports_the_exact_named_harnesses() -> None:
    """Both named positive harnesses are reported as verified."""
    report = parse_proof_report(_fixture("positive.txt"))

    assert report.outcomes == (
        HarnessOutcome("bounded_push_contents_are_exact", verified=True),
        HarnessOutcome("raii_release_runs_once", verified=True),
    )
    assert (report.verified_count, report.failed_count, report.total_count) == (2, 0, 2)


def test_the_threaded_positive_transcript_is_accepted() -> None:
    """A parallel run prefixes some lines with a thread marker."""
    report = parse_proof_report(_fixture("threaded_positive.txt"))

    assert report.verified_names == {
        "bounded_push_contents_are_exact",
        "raii_release_runs_once",
    }


def test_the_false_assertion_is_a_verification_failure() -> None:
    """The negative fixture must be a failed proof, not a failed compile."""
    report = require_named_harnesses(
        _fixture("false_assertion.txt"),
        verified=(),
        failed=("deliberately_false_assertion",),
    )

    assert report.failed_names == {"deliberately_false_assertion"}
    assert report.verified_names == frozenset()


def test_a_compile_error_is_not_a_verification_failure() -> None:
    """A build failure must not be accepted as the deliberately false proof."""
    with pytest.raises(ProofCompileError, match="compile"):
        parse_proof_report(_fixture("compile_error.txt"))


def test_a_solver_failure_is_not_a_verification_failure() -> None:
    """A CBMC failure must not be accepted as the deliberately false proof."""
    with pytest.raises(ProofSolverError, match="CBMC"):
        parse_proof_report(_fixture("solver_failure.txt"))


def test_a_zero_harness_run_is_refused() -> None:
    """A run that found no proof harnesses proves nothing."""
    with pytest.raises(ProofTranscriptError, match="no proof harnesses"):
        parse_proof_report(_fixture("zero_harnesses.txt"))


def test_a_truncated_transcript_is_refused() -> None:
    """A harness start with no verdict is not a result."""
    with pytest.raises(ProofTranscriptError, match="verdict"):
        parse_proof_report(_fixture("truncated.txt"))


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("contradictory.txt", "summary"),
        ("duplicate_verdict.txt", "verdict"),
        ("no_summary.txt", "summary"),
    ],
)
def test_inconsistent_transcripts_are_refused(name: str, message: str) -> None:
    """Each malformed shape fails for its own stated reason."""
    with pytest.raises(ProofTranscriptError, match=message):
        parse_proof_report(_fixture(name))


def test_an_oversized_transcript_is_refused_before_parsing() -> None:
    """A bounded reader does not load an unbounded tool log."""
    transcript = "x" * (MAX_TRANSCRIPT_BYTES + 1)

    with pytest.raises(ProofTranscriptError, match="ceiling"):
        parse_proof_report(transcript)


def test_require_named_harnesses_refuses_a_missing_harness() -> None:
    """A set that omits one required harness is not the expected set."""
    with pytest.raises(ProofTranscriptError, match="did not verify"):
        require_named_harnesses(
            _fixture("positive.txt"),
            verified=("bounded_push_contents_are_exact", "absent_harness"),
            failed=(),
        )


def test_require_named_harnesses_refuses_an_extra_harness() -> None:
    """An unexpected successful harness is refused rather than ignored."""
    with pytest.raises(ProofTranscriptError, match="unexpected"):
        require_named_harnesses(
            _fixture("positive.txt"),
            verified=("bounded_push_contents_are_exact",),
            failed=(),
        )


def test_require_named_harnesses_refuses_the_wrong_status() -> None:
    """A harness that failed cannot satisfy an expectation that it verified."""
    with pytest.raises(ProofTranscriptError, match="expected to verify"):
        require_named_harnesses(
            _fixture("false_assertion.txt"),
            verified=("deliberately_false_assertion",),
            failed=(),
        )


def test_qualified_names_match_on_their_final_component() -> None:
    """Kani prints a harness inside a module with its module path."""
    transcript = _transcript(("simple_lib::kani_tests::test_sum",), ())

    report = require_named_harnesses(
        transcript, verified=("test_sum",), failed=(), qualified=True
    )

    assert report.verified_names == {"simple_lib::kani_tests::test_sum"}


def test_qualified_matching_keeps_the_exact_set() -> None:
    """A qualified run still refuses an extra or missing harness."""
    transcript = _transcript(
        (
            "simple_lib::kani_tests::test_sum",
            "simple_lib::pair::kani_tests::test_one_plus_two",
        ),
        (),
    )

    with pytest.raises(ProofTranscriptError, match="unexpected"):
        require_named_harnesses(
            transcript, verified=("test_sum",), failed=(), qualified=True
        )


def test_two_qualified_harnesses_with_one_name_are_refused() -> None:
    """Collapsing a module path must not merge two distinct harnesses."""
    transcript = _transcript(
        ("simple_lib::kani_tests::test_sum", "other::test_sum", "other::test_two"), ()
    )

    with pytest.raises(ProofTranscriptError, match="ambiguous"):
        require_named_harnesses(
            transcript, verified=("test_sum",), failed=(), qualified=True
        )


def test_a_wrong_module_cannot_satisfy_an_expected_path() -> None:
    """A harness in another module is a different harness, not a match."""
    transcript = _transcript(
        ("other::test_sum", "simple_lib::kani_tests::test_two"), ()
    )

    with pytest.raises(ProofTranscriptError, match="did not verify"):
        require_named_harnesses(
            transcript,
            verified=("simple_lib::kani_tests::test_sum",),
            failed=(),
            qualified=True,
        )


def test_an_expected_path_matches_its_reported_prefix() -> None:
    """A full expected path matches a report that carries it as a suffix."""
    transcript = _transcript(("simple_lib::kani_tests::test_sum",), ())

    require_named_harnesses(
        transcript,
        verified=("kani_tests::test_sum",),
        failed=(),
        qualified=True,
    )


def test_the_rendered_report_is_stable(snapshot: SnapshotAssertion) -> None:
    """The rendered multivariant output is pinned by a Syrupy snapshot."""
    assert render_proof_report(parse_proof_report(_fixture("positive.txt"))) == snapshot


def test_the_rendered_failure_is_stable(snapshot: SnapshotAssertion) -> None:
    """The failure rendering is pinned separately from the success form."""
    assert (
        render_proof_report(parse_proof_report(_fixture("false_assertion.txt")))
        == snapshot
    )


@given(
    verified=st.sets(_NAMES, max_size=4),
    failed=st.sets(_NAMES, max_size=4),
)
@settings(max_examples=30, deadline=None)
def test_generated_transcripts_round_trip_through_the_validator(
    verified: set[str], failed: set[str]
) -> None:
    """The parser agrees with an independent model of a well-formed run.

    A run with no harness at all is excluded here because it is refused
    separately: no verified and no failed harness is the zero-harness shape.
    """
    failed = failed.difference(verified)
    if not verified and not failed:
        return
    report = require_named_harnesses(
        _transcript(sorted(verified), sorted(failed)),
        verified=verified,
        failed=failed,
    )

    assert report.verified_names == verified
    assert report.failed_names == failed
    assert report.total_count == len(verified) + len(failed)


@given(verified=st.sets(_NAMES, min_size=1, max_size=4))
@settings(max_examples=20, deadline=None)
def test_a_dropped_harness_is_refused(verified: set[str]) -> None:
    """A harness missing from the transcript is detected, not ignored.

    One harness is always retained, so the refusal names the missing harness
    rather than reporting the separate zero-harness shape.
    """
    missing = min(verified)
    retained = sorted(verified - {missing})[:1] or ["retained-harness"]
    transcript = _transcript(retained, ())
    expectation = frozenset(retained) | {missing}

    with pytest.raises(ProofTranscriptError, match="did not verify"):
        require_named_harnesses(transcript, verified=expectation, failed=())


def test_the_false_transcript_names_the_deliberately_false_check() -> None:
    """The negative control must fail for its own assertion, not another one."""
    transcript = _fixture("false_assertion.txt")

    assert "Failed Checks: deliberately false" in transcript
