"""Hold the incremental Kani reader to the corpus and its accepted facts."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_log import (
    MAX_RETAINED_ISSUES,
    KaniLog,
    KaniLogError,
    KaniLogReader,
    UnsupportedKaniGrammarError,
    parse,
)
from rust_prover_tools.unittests.kani_log_support import (
    HARNESS_START,
    LOGS,
    REPORT_PREFIXES,
    TIME,
    VERIFICATION,
    assert_matches_expected,
    lines_of,
    log_text,
)


@pytest.mark.parametrize("name", sorted(LOGS))
def test_the_corpus_reports_the_committed_facts(name: str) -> None:
    """Every accepted corpus log parses to exactly its recorded facts."""
    entry = LOGS[name]
    if "refused" in entry:
        with pytest.raises(KaniLogError) as raised:
            parse(log_text(name))
        assert entry["refused"] in raised.value.reason
        return
    assert_matches_expected(name, parse(log_text(name)))


@pytest.mark.parametrize("name", sorted(LOGS))
def test_feed_line_matches_the_whole_text_parse(name: str) -> None:
    """Feeding one line at a time yields the whole-text result or refusal."""
    reader = KaniLogReader()
    for line in lines_of(name):
        reader.feed_line("stdout", line)
    fed = _finish(reader)
    if isinstance(fed, str):
        with pytest.raises(KaniLogError) as whole:
            parse(log_text(name))
        assert fed == str(whole.value)
        return
    assert fed == parse(log_text(name)), "line-fed facts diverged"


def _finish(reader: KaniLogReader) -> KaniLog | str:
    """Finish a reader, returning its facts or the reason it refused."""
    try:
        return reader.finish(0)
    except KaniLogError as error:
        return str(error)


def test_the_reader_retains_selection_evidence_after_truncation() -> None:
    """A stopped proof keeps the identity it had already announced."""
    lines = lines_of("verified")
    time_index = next(
        index for index, line in enumerate(lines) if line.startswith(TIME)
    )
    reader = KaniLogReader()
    for line in lines[:time_index]:
        reader.feed_line("stdout", line)
    facts = reader.facts
    assert facts.selection_records == ("harnesses::verified",), (
        "the selection record was lost"
    )
    assert facts.verdicts == (("harnesses::verified", "SUCCESSFUL"),), (
        "the verdict was lost"
    )
    assert not facts.completed, "a truncated log reported completion"
    assert facts.truncated, "a truncated log did not report truncation"
    assert facts.last_progress is not None, "the last progress line was lost"
    with pytest.raises(KaniLogError):
        reader.finish(1)


def test_an_unregistered_release_is_refused_by_type() -> None:
    """A banner from another release names the release and the registered set."""
    text = log_text("verified").replace(
        "Kani Rust Verifier 0.67.0", "Kani Rust Verifier 0.68.0", 1
    )
    with pytest.raises(UnsupportedKaniGrammarError) as raised:
        parse(text)
    assert raised.value.version == "0.68.0"
    assert raised.value.supported == ("0.67.0",)


def test_standard_error_preamble_before_the_banner_is_tolerated() -> None:
    """Cargo writes to standard error before Kani writes its banner."""
    reader = KaniLogReader()
    reader.feed_line("stderr", "warning: unused config key `unstable.embed-metadata`")
    for line in lines_of("verified"):
        reader.feed_line("stdout", line)
    assert_matches_expected("verified", reader.finish(0))


def test_a_line_without_a_banner_is_refused() -> None:
    """A log that never opens with a banner cannot be attributed to a release."""
    with pytest.raises(KaniLogError, match="does not begin with a Kani banner"):
        parse("not a log")


def test_an_inventory_log_parses_without_verdicts() -> None:
    """A ``kani list`` run is a log with a format and no blocks."""
    log = parse(log_text("list"))
    assert log.format == "inventory"
    assert not log.blocks
    assert log.manual is None
    assert log.unsupported_constructs == ("TerminatorKind::InlineAsm",)


def test_an_inventory_records_the_supplied_exit_status() -> None:
    """The raw child status travels with the parsed facts."""
    assert parse(log_text("verified"), exit_code=1).exit_code == 1


def _chatter() -> st.SearchStrategy[str]:
    """Generate a line that cannot be mistaken for report structure."""
    return st.text(min_size=1, max_size=40).filter(
        lambda line: (
            line.splitlines() == [line]
            and line.strip()
            and not line.startswith(REPORT_PREFIXES)
            and not line.startswith("\t")
        )
    )


@settings(max_examples=40, deadline=None)
@given(st.lists(_chatter(), min_size=1, max_size=6))
def test_arbitrary_chatter_between_reports_is_accepted(noise: list[str]) -> None:
    """Chatter between reports must not change the facts or the outcome."""
    lines = lines_of("verified")
    banner, rest = lines[0], lines[1:]
    report_start = next(
        index for index, line in enumerate(rest) if line.startswith(HARNESS_START)
    )
    noisy = [banner, *rest[:report_start], *noise, *rest[report_start:]]
    parsed: typ.Final[KaniLog] = parse("\n".join(noisy))
    assert parsed.harnesses == ("harnesses::verified",) * 1


def test_the_verdict_note_marks_an_expected_panic() -> None:
    """A successful expected panic keeps its note as a separate fact."""
    log = parse(log_text("expected_panic"))
    (block,) = log.blocks
    assert block.succeeded
    assert block.panics_as_expected
    assert block.verdict == "SUCCESSFUL"


def test_a_verdict_without_the_note_is_not_an_expected_panic() -> None:
    """An ordinary success must not be read as an excused panic."""
    log = parse(log_text("verified"))
    (block,) = log.blocks
    assert block.succeeded
    assert not block.panics_as_expected
    assert VERIFICATION in log_text("verified")


def test_retained_issues_are_bounded_without_losing_the_report() -> None:
    """A noisy harness cannot grow the parser state without bound."""
    lines = lines_of("verified")
    reader = KaniLogReader()
    reader.feed_line("stdout", lines[0])
    reader.feed_line(
        "stdout",
        next(line for line in lines if line.startswith(HARNESS_START)),
    )
    reader.feed_line("stdout", "RESULTS:")
    for index in range(MAX_RETAINED_ISSUES + 10):
        reader.feed_line("stdout", f"unexplained build chatter {index}")

    issues = reader.retained_issues
    facts = reader.facts

    assert len(issues) == MAX_RETAINED_ISSUES + 1
    assert "dropped" in issues[-1].message
    assert facts.banner_version == "0.67.0"
    assert facts.selection_records == ("harnesses::verified",)
    with pytest.raises(KaniLogError, match="unrecognised line"):
        reader.finish(0)


def test_a_second_banner_after_the_summary_is_refused() -> None:
    """A capture cannot contain a second, partial invocation."""
    text = "\n".join((
        *lines_of("verified"),
        "Kani Rust Verifier 0.68.0 (cargo plugin)",
    ))

    with pytest.raises(KaniLogError, match="second Kani banner"):
        parse(text)


def test_a_unicode_harness_name_is_selected() -> None:
    """Kani prints a Unicode pretty name unchanged."""
    text = log_text("verified").replace("harnesses::verified", "proofs::vérifie")

    assert parse(text).harnesses == ("proofs::vérifie",)


def test_a_block_after_the_final_summary_is_refused() -> None:
    """The final summary cannot precede evidence it claims to summarise."""
    text = "\n".join((
        *lines_of("verified"),
        "Checking harness harnesses::late...",
    ))

    with pytest.raises(KaniLogError, match="after the final manual summary"):
        parse(text)
