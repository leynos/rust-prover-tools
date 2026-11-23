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
    """The streaming entry point yields exactly the batch facts."""
    text = (_CORPUS / "verified.log").read_text(encoding="utf-8")
    lines = [line.removesuffix("\r") for line in text.split("\n")]

    streamed = parse_kani_log_lines(iter(lines))

    assert streamed.harnesses == parse_kani_log(text).harnesses


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
