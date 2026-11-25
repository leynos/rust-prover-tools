"""Hold the bounded line splitter to chunk boundaries and overflow behaviour."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

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


def test_invalid_bytes_cannot_exceed_the_encoded_limit() -> None:
    """Replacement characters count against the encoded budget."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    (line,) = splitter.feed(b"\xff" * 64 + b"\n")

    assert len(line.encode("utf-8")) <= 64
    assert "truncated" in line


def test_invalid_bytes_at_eof_cannot_exceed_the_limit() -> None:
    """The EOF path applies the same encoded bound."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    splitter.feed(b"\xff" * 64)
    (line,) = splitter.finish()

    assert len(line.encode("utf-8")) <= 64


def test_a_valid_wide_line_stays_within_the_encoded_limit() -> None:
    """A line of multi-byte characters is bounded by encoded length."""
    splitter = BoundedLineSplitter(max_line_bytes=64)

    (line,) = splitter.feed("é".encode() * 100 + b"\n")

    assert len(line.encode("utf-8")) <= 64


@given(
    st.lists(st.binary(min_size=0, max_size=48), max_size=8),
    st.integers(min_value=64, max_value=256),
)
@settings(max_examples=50, deadline=None)
def test_the_chunk_split_does_not_change_the_emitted_lines(
    chunks: list[bytes],
    limit: int,
) -> None:
    """Splitting the same bytes differently emits the same bounded lines."""
    split = BoundedLineSplitter(max_line_bytes=limit)
    emitted: list[str] = []
    for chunk in chunks:
        emitted.extend(split.feed(chunk))
    emitted.extend(split.finish())

    whole = BoundedLineSplitter(max_line_bytes=limit)
    expected = [*whole.feed(b"".join(chunks)), *whole.finish()]

    assert emitted == expected
    assert all(len(line.encode("utf-8")) <= limit for line in emitted)
