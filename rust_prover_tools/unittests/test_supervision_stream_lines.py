"""Hold the bounded line splitter to chunk boundaries and overflow behaviour."""

from __future__ import annotations

from rust_prover_tools.supervision.stream_lines import BoundedLineSplitter


def test_crlf_is_one_boundary() -> None:
    """A CRLF terminator finishes one line, not two."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    assert splitter.feed(b"first\r\nsecond\n") == ("first", "second")


def test_a_boundary_split_across_chunks_is_retained() -> None:
    """A trailing CR waits for the next chunk to decide CR or CRLF."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    assert splitter.feed(b"first\r") == ()
    assert splitter.feed(b"\nsecond") == ("first",)
    assert splitter.finish() == ("second",)


def test_a_long_line_is_truncated_and_the_rest_discarded() -> None:
    """A newline-free line cannot grow the buffer without bound."""
    splitter = BoundedLineSplitter(max_line_bytes=8)

    first = splitter.feed(b"x" * 100)
    rest = splitter.feed(b"y" * 100 + b"\nnext\n")

    assert len(first) == 1
    assert first[0].startswith("x" * 8)
    assert "truncated" in first[0]
    assert rest == ("next",)


def test_the_overflow_marker_names_the_budget() -> None:
    """The truncation marker states the bound that was exceeded."""
    splitter = BoundedLineSplitter(max_line_bytes=4)

    (line,) = splitter.feed(b"abcdef")

    assert "exceeded 4 bytes" in line


def test_finish_emits_a_bounded_trailing_line() -> None:
    """A stream that ends without a terminator still reports its tail."""
    splitter = BoundedLineSplitter(max_line_bytes=16)

    assert splitter.feed(b"partial") == ()
    assert splitter.finish() == ("partial",)
    assert splitter.finish() == ()


def test_undecodable_bytes_are_replaced() -> None:
    """One malformed byte cannot end the run."""
    splitter = BoundedLineSplitter(max_line_bytes=16)

    (line,) = splitter.feed(b"ok \xff\n")

    assert line.startswith("ok ")
    assert "�" in line


def test_a_discarding_stream_drops_the_rest_of_the_line() -> None:
    """Overflow state ends only at the next terminator."""
    splitter = BoundedLineSplitter(max_line_bytes=4)

    splitter.feed(b"abcdef")
    assert splitter.feed(b"ghij") == ()
    assert splitter.finish() == ()
    assert splitter.feed(b"\nafter\n") == ("after",)
