"""Hold the shared parser's streaming line source to bounded behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_a_long_blank_run_fails_closed() -> None:
    """Lookahead past the retained window is refused, not buffered forever."""
    from rust_prover_tools.kani_log.lines import MAX_WINDOW_LINES

    lines = ["Kani Rust Verifier 0.67.0 (cargo plugin)"]
    lines.extend("" for _ in range(MAX_WINDOW_LINES + 10))
    lines.append("Checking harness h...")

    with pytest.raises(KaniLogError):
        parse_kani_log_lines(iter(lines))


def test_the_window_is_bounded_for_streaming_input() -> None:
    """A streaming source refuses an index beyond its configured window."""
    source = StreamingLines(iter(f"line {index}" for index in range(100)), max_window=8)

    with pytest.raises(KaniLogError, match="lookahead"):
        source.at(20)
    assert source.buffered == 0
