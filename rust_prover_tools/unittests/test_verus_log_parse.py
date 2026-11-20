"""Prove the Verus parser reads real logs and refuses contradictory ones."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rust_prover_tools.unittests.conftest import DEFAULT_INT_DIGIT_LIMIT
from rust_prover_tools.verus_log import (
    SUPPORTED_VERUS_VERSIONS,
    VerusFailure,
    VerusLogError,
    parse_verus_log,
)
from tests import verus_log_corpus as corpus

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "verus_logs"
)
CORPORA: typ.Final[tuple[Path, ...]] = tuple(
    FIXTURES / version for version in SUPPORTED_VERUS_VERSIONS
)
CURRENT_CORPUS: typ.Final[Path] = CORPORA[0]

#: Lines the scan must skip.  None of them is a summary, a diagnostic header
#: or a selection note, so each is compiler chatter the parser has no business
#: reading a fact from.
_CHATTER = st.sampled_from(
    (
        "note: running Verus",
        "warning: unused variable: `unused`",
        "  |",
        "   = help: consider adding a specification",
        "Completed verification",
        "",
        "\t",
    ),
)


def log_text(name: str) -> str:
    """Return one committed log exactly as the parser receives it."""
    return (CURRENT_CORPUS / f"{name}.log").read_text(encoding="utf-8")


def failure_facts(
    failure: VerusFailure,
) -> tuple[str, str | None, int | None, int | None]:
    """Reduce one failure to the facts the corpus records."""
    return (failure.message, failure.path, failure.line, failure.column)


CORPUS_CASES: typ.Final[tuple[tuple[str, str], ...]] = tuple(
    (version, name)
    for version in SUPPORTED_VERUS_VERSIONS
    for name in sorted(path.stem for path in (FIXTURES / version).glob("*.log"))
)


@pytest.mark.parametrize(("version", "name"), CORPUS_CASES)
def test_a_committed_log_parses_to_the_facts_it_records(
    version: str, name: str
) -> None:
    """Every committed log yields exactly the facts the corpus records."""
    recorded = corpus.expected()["logs"][name]
    log = parse_verus_log(
        (FIXTURES / version / f"{name}.log").read_text(encoding="utf-8"),
    )

    assert (log.summary is None) is (recorded["summary"] is None)
    if log.summary is not None:
        assert log.summary.verified == recorded["summary"]["verified"]
        assert log.summary.errors == recorded["summary"]["errors"]
        assert log.summary.is_partial is recorded["summary"]["partial"]
    assert log.selection == recorded["selection"]
    assert [failure_facts(item) for item in log.failures] == [
        (item["message"], item["path"], item["line"], item["column"])
        for item in recorded["failures"]
    ]


def test_the_corpus_names_one_qualified_release() -> None:
    """The corpus directory is the release the parser claims to support."""
    assert (CURRENT_CORPUS / "expected.json").is_file()
    assert corpus.expected()["release"] == SUPPORTED_VERUS_VERSIONS[0]


def test_the_merged_stream_capture_matches_the_sequence_it_merges() -> None:
    """A capture that merged stdout after stderr parses to the same facts.

    Verus writes the summary to standard output and the diagnostic to standard
    error, so the same run can be captured in either order.
    """
    merged = parse_verus_log(log_text("split_streams"))
    ordered = parse_verus_log(log_text("postcondition"))

    assert merged.summary == ordered.summary
    assert merged.failures == ordered.failures


def test_two_summaries_are_refused() -> None:
    """A log cannot report two verification results."""
    text = "\n".join(
        (
            "verification results:: 2 verified, 0 errors",
            "verification results:: 1 verified, 1 errors",
        ),
    )
    with pytest.raises(VerusLogError, match="two verification results summaries"):
        parse_verus_log(text)


def test_a_clean_summary_beside_a_failure_is_refused() -> None:
    """A clean summary cannot sit beside a diagnostic it should have counted."""
    text = "\n".join(
        (
            "error: postcondition not satisfied",
            "  --> two_errors.rs:8:13",
            "verification results:: 1 verified, 0 errors",
        ),
    )
    with pytest.raises(VerusLogError, match="records no verification errors"):
        parse_verus_log(text)


def test_a_log_with_no_summary_keeps_the_diagnostics_it_found() -> None:
    """A usage error names no summary but still reports a diagnostic."""
    log = parse_verus_log(log_text("usage_error"))

    assert log.summary is None
    assert len(log.failures) == 1
    assert log.failures[0].is_located is False


def test_an_oversized_count_is_a_structural_error(
    default_int_digit_limit: object,
) -> None:
    """A digit run past the interpreter limit is refused, not raised raw."""
    digits = "9" * (DEFAULT_INT_DIGIT_LIMIT + 1)
    text = f"verification results:: {digits} verified, 0 errors"

    with pytest.raises(VerusLogError, match="not a count this parser can read"):
        parse_verus_log(text)


def test_a_span_with_unreadable_numbers_keeps_the_diagnostic(
    default_int_digit_limit: object,
) -> None:
    """An unreadable span locates nothing but must not lose the failure.

    The function still failed; only its position is unusable.  Dropping the
    diagnostic would turn a reported verification failure into a clean run.
    """
    digits = "9" * (DEFAULT_INT_DIGIT_LIMIT + 1)
    text = "\n".join(
        (
            "error: assertion failed",
            f"  --> huge.rs:{digits}:1",
            "verification results:: 0 verified, 1 errors",
        ),
    )

    log = parse_verus_log(text)

    assert [failure_facts(item) for item in log.failures] == [
        ("assertion failed", None, None, None),
    ]


@given(
    chatter=_CHATTER,
    before=st.integers(min_value=0, max_value=1),
    after=st.integers(min_value=0, max_value=1),
)
def test_chatter_around_a_summary_changes_nothing(
    chatter: str,
    before: int,
    after: int,
) -> None:
    """Lines the grammar does not name are skipped wherever they appear.

    Chatter is kept outside the pair of the diagnostic and its span: rustc
    prints those on consecutive lines, and a capture never separates them.
    """
    head = ["error: postcondition not satisfied", "  --> proofs/mutant.rs:8:13"]
    inserted = [*(chatter for _ in range(before)), *head]
    inserted.extend(chatter for _ in range(after))
    inserted.append("verification results:: 1 verified, 1 errors")

    log = parse_verus_log("\n".join(inserted))

    assert log.summary is not None
    assert (log.summary.verified, log.summary.errors) == (1, 1)
    assert [failure_facts(item) for item in log.failures] == [
        ("postcondition not satisfied", "proofs/mutant.rs", 8, 13),
    ]


@given(chatter=_CHATTER)
def test_the_summary_is_found_before_or_after_the_diagnostic(chatter: str) -> None:
    """The scan reports the same facts whichever stream came first."""
    summary = "verification results:: 1 verified, 1 errors"
    diagnostic = ("error: postcondition not satisfied", "  --> proofs/mutant.rs:8:13")

    first = parse_verus_log("\n".join((summary, chatter, *diagnostic)))
    second = parse_verus_log("\n".join((*diagnostic, chatter, summary)))

    assert first.summary == second.summary
    assert first.failures == second.failures
