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
    splitter = BoundedLineSplitter(max_line_bytes=64)

    first = splitter.feed(b"x" * 100)
    rest = splitter.feed(b"y" * 100 + b"\nnext\n")

    assert len(first) == 1
    assert first[0].startswith("x" * 8)
    assert "truncated" in first[0]
    assert rest == ("next",)


def test_the_overflow_marker_names_the_budget() -> None:
    """The truncation marker states the bound that was exceeded."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    (line,) = splitter.feed(b"x" * 200)

    assert "exceeded 64 bytes" in line


def test_finish_emits_a_bounded_trailing_line() -> None:
    """A stream that ends without a terminator still reports its tail."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    assert splitter.feed(b"partial") == ()
    assert splitter.finish() == ("partial",)
    assert splitter.finish() == ()


def test_undecodable_bytes_are_replaced() -> None:
    """One malformed byte cannot end the run."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    (line,) = splitter.feed(b"ok \xff\n")

    assert line.startswith("ok ")
    assert "�" in line


def test_a_discarding_stream_drops_the_rest_of_the_line() -> None:
    """Overflow state ends only at the next terminator."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    splitter.feed(b"x" * 200)
    assert splitter.feed(b"ghij") == ()
    assert splitter.finish() == ()
    assert splitter.feed(b"\nafter\n") == ("after",)


def test_a_terminator_in_the_same_chunk_still_truncates() -> None:
    """An over-long line followed by a newline is truncated, not emitted whole."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    lines = splitter.feed(b"x" * 200 + b"\nnext\n")

    assert len(lines) == 2
    assert "truncated" in lines[0]
    assert lines[0].startswith("x")
    assert lines[1] == "next"


def test_the_partial_buffer_never_exceeds_the_limit() -> None:
    """The bound applies while bytes accumulate, not only after the chunk."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    splitter.feed(b"x" * 200)
    assert len(splitter._pending) <= 64


def test_incomplete_utf8_at_a_newline_is_replaced() -> None:
    """An incomplete sequence does not leak into the next line."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    first, second = splitter.feed(b"bad \xe2\x82\nok\n")

    assert first.startswith("bad ")
    assert first.count("\ufffd") == 1
    assert second == "ok"


def test_incomplete_utf8_at_eof_is_replaced() -> None:
    """An incomplete trailing sequence is reported, not lost."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    splitter.feed(b"tail \xe2\x82")
    (line,) = splitter.finish()

    assert line.startswith("tail ")
    assert line.count("\ufffd") == 1


def test_incomplete_utf8_at_truncation_is_replaced() -> None:
    """A truncated line ending mid-sequence still decodes."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    (line,) = splitter.feed(b"x" * 10 + b"\xe2" + b"z" * 60)

    assert "truncated" in line
    assert "\ufffd" in line
    assert len(line.encode("utf-8")) <= 64
