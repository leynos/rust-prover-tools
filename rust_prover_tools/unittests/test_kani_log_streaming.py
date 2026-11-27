"""Hold the shared parser's streaming line source to bounded behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_log import (
    KaniLogError,
    parse_kani_log,
    parse_kani_log_lines,
)
from rust_prover_tools.kani_log.reader import StreamingLines

_CORPUS = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs" / "0.67.0"
)


def test_streaming_parse_matches_the_batch_parse() -> None:
    """Every corpus log yields the same facts or the same refusal."""
    for path in sorted(_CORPUS.glob("*.log")):
        text = path.read_text(encoding="utf-8")
        lines = [line.removesuffix("\r") for line in text.split("\n")]
        try:
            batch = parse_kani_log(text)
        except KaniLogError as error:
            batch_refusal = str(error)
        else:
            assert parse_kani_log_lines(iter(lines)) == batch
            continue
        with pytest.raises(KaniLogError) as streamed:
            parse_kani_log_lines(iter(lines))
        assert str(streamed.value) == batch_refusal


def test_the_window_stays_small_while_parsing() -> None:
    """The source releases consumed lines instead of growing with the log."""
    lines = [f"line {index}" for index in range(10_000)]
    source = StreamingLines(iter(lines))
    source.at(0)
    source.release(1)
    for index in range(1, 200):
        source.at(index)
        source.release(index + 1)

    assert source.buffered <= 2


def test_a_released_line_cannot_be_read_again() -> None:
    """Reading behind the released window is refused, not guessed."""
    source = StreamingLines(iter(("a", "b")))
    source.at(0)
    source.release(2)

    with pytest.raises(KaniLogError, match="released"):
        source.at(0)


def _framed_report(blanks: int) -> list[str]:
    """Return a complete report whose description spans a run of blank lines.

    The description's first continuation line ends in a quote, so whether the
    frame has closed is decided by the next non-blank line. With a blank run
    after it, that decision is the parser's only multi-line lookahead.
    """
    return [
        "Kani Rust Verifier 0.67.0 (cargo plugin)",
        "Checking harness h0...",
        "RESULTS:",
        "Check 1: h0.assertion.1",
        "\t - Status: SUCCESS",
        '\t - Description: "opening',
        'middle"',
        *("" for _ in range(blanks)),
        "closing text",
        'last"',
        "\t - Location: src/x.rs:1:1 in function h0",
        "",
        "SUMMARY:",
        " ** 0 of 1 failed",
        "",
        "VERIFICATION:- SUCCESSFUL",
        "Verification Time: 0.1s",
        "",
        "Manual Harness Summary:",
        "Complete - 1 successfully verified harnesses, 0 failures, 1 total.",
    ]


def test_a_blank_run_inside_the_window_is_read_in_full() -> None:
    """A description spanning fewer blank lines than the window parses."""
    from rust_prover_tools.kani_log.lines import MAX_WINDOW_LINES

    log = parse_kani_log_lines(iter(_framed_report(MAX_WINDOW_LINES - 20)))

    (block,) = log.blocks
    description = block.properties[0].description
    assert description.startswith('opening\nmiddle"\n')
    assert description.endswith("\nclosing text\nlast")


def test_a_blank_run_past_the_window_is_a_lookahead_refusal() -> None:
    """Lookahead past the retained window is refused, not buffered forever."""
    from rust_prover_tools.kani_log.lines import MAX_WINDOW_LINES

    with pytest.raises(KaniLogError, match="lookahead"):
        parse_kani_log_lines(iter(_framed_report(MAX_WINDOW_LINES + 10)))


def test_an_oversized_line_in_description_lookahead_is_refused() -> None:
    """The line budget binds the lines lookahead itself walks past.

    A description ending in a quote is closed only by the next non-blank line,
    so `_blank_closes_entry` walks every blank line between them with
    ``peek_at``. Replacing the *second* blank line keeps the first under the
    cursor's own read, so the refusal can only come from that walk. The
    reported line number is asserted, which pins which line was refused.
    """
    from rust_prover_tools.kani_log.lines import MAX_LINE_CHARS

    lines = list(_framed_report(2))
    first_blank = next(index for index, line in enumerate(lines) if line == "")
    oversized = first_blank + 1
    lines[oversized] = " " * (MAX_LINE_CHARS + 1)

    with pytest.raises(KaniLogError, match="line budget") as refused:
        parse_kani_log_lines(iter(lines))

    assert f"line {oversized + 1} is longer" in str(refused.value)


def test_the_window_is_bounded_for_streaming_input() -> None:
    """A streaming source refuses an index beyond its configured window."""
    source = StreamingLines(iter(f"line {index}" for index in range(100)), max_window=8)

    with pytest.raises(KaniLogError, match="lookahead"):
        source.at(20)
    assert source.buffered == 0


def test_aggregate_lookahead_memory_is_bounded() -> None:
    """Many long lines cannot accumulate past the retention budget.

    The window bounds the count and the line budget bounds one line; without
    an aggregate bound, 4,096 lines at the line limit would retain about
    16 GiB. The source refuses while reading the line that would exceed the
    aggregate, before the description budget or the frame decision applies.
    """
    from rust_prover_tools.kani_log.lines import (
        MAX_BUFFERED_CHARS,
        MAX_LINE_CHARS,
    )

    line = " " * MAX_LINE_CHARS
    lines = [line] * (MAX_BUFFERED_CHARS // MAX_LINE_CHARS + 1)
    source = StreamingLines(iter(lines))

    with pytest.raises(KaniLogError, match="retention budget"):
        source.at(len(lines) - 1)


@given(
    st.lists(st.text(max_size=12), min_size=1, max_size=12),
    st.integers(min_value=1, max_value=6),
)
@settings(max_examples=50, deadline=None)
def test_reads_inside_the_window_succeed_and_released_lines_are_refused(
    lines: list[str],
    window: int,
) -> None:
    """A generated source reads its window, refuses behind it and beyond it."""
    source = StreamingLines(iter(lines), max_window=window)

    for index in range(min(len(lines), window)):
        assert source.at(index) == lines[index]
    assert source.buffered <= window

    source.release(1)
    with pytest.raises(KaniLogError, match="released"):
        source.at(0)
    with pytest.raises(KaniLogError, match="lookahead"):
        source.at(window + 1)
