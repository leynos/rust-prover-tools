"""Split raw child bytes into bounded lines without an unbounded partial buffer.

Cuprum's line hook accumulates an unterminated line in its own drain buffer
before invoking the hook, and its public options expose no chunk-level bound.
The supervisor therefore owns the child's pipes and splits them here, where the
partial line is bounded: once it exceeds the configured budget the prefix is
forwarded with an explicit marker and the rest of that line is discarded until
its terminator arrives.

The splitter mirrors Cuprum's rule for CR, LF, and CRLF boundaries so a line a
real verifier writes is forwarded whole rather than split on a carriage return.
"""

from __future__ import annotations

import dataclasses as dc

__all__ = ("BoundedLineSplitter",)

#: Line-feed and carriage-return byte values.
_LF = 0x0A
_CR = 0x0D


@dc.dataclass(slots=True)
class BoundedLineSplitter:
    """Turn raw byte chunks into complete lines with a bounded partial buffer.

    Attributes
    ----------
    max_line_bytes : int
        Largest partial line retained. A longer line is truncated with a
        marker and the remainder discarded until its terminator.
    encoding : str
        Encoding used to decode emitted lines.
    errors : str
        Decoding error policy. The default replaces undecodable bytes so one
        malformed byte cannot end the run.
    """

    max_line_bytes: int
    encoding: str = "utf-8"
    errors: str = "replace"

    def __post_init__(self) -> None:
        """Refuse a budget too small to carry the truncation marker."""
        marker = self._marker()
        if self.max_line_bytes < len(marker.encode("utf-8")):
            msg = (
                "max_line_bytes must be at least the truncation marker's "
                f"length ({len(marker.encode('utf-8'))})"
            )
            raise ValueError(msg)

    def _marker(self) -> str:
        """Return the marker appended to an over-long line."""
        return f"…[truncated: line exceeded {self.max_line_bytes} bytes]"

    _pending: bytearray = dc.field(default_factory=bytearray, init=False)
    _discarding: bool = dc.field(default=False, init=False)
    _pending_cr: bool = dc.field(default=False, init=False)
    _skip_lf: bool = dc.field(default=False, init=False)

    def feed(self, chunk: bytes) -> tuple[str, ...]:
        """Return the complete lines one chunk finished.

        Parameters
        ----------
        chunk : bytes
            Raw bytes read from the child.

        Returns
        -------
        tuple[str, ...]
            Complete lines, without their terminators, in arrival order.
        """
        lines: list[str] = []
        for index, byte in enumerate(chunk):
            self._consume(byte, last=index + 1 == len(chunk), lines=lines)
        return tuple(lines)

    def _consume(self, byte: int, *, last: bool, lines: list[str]) -> None:
        """Consume one byte, finishing a line when it is a terminator."""
        if self._skip_boundary_byte(byte):
            return
        if self._pending_cr:
            self._pending_cr = False
            self._boundary(lines)
            if byte == _LF:
                return
        if byte == _CR:
            self._at_cr(last=last, lines=lines)
        elif byte == _LF:
            self._boundary(lines)
        else:
            self._append(byte, lines)

    def _skip_boundary_byte(self, byte: int) -> bool:
        """Skip an LF already consumed as the second half of CRLF."""
        if not self._skip_lf:
            return False
        self._skip_lf = False
        return byte == _LF

    def _append(self, byte: int, lines: list[str]) -> None:
        """Append one ordinary byte, enforcing the bound while consuming."""
        if self._discarding:
            return
        self._pending.append(byte)
        if len(self._pending) > self.max_line_bytes:
            # Waiting until the chunk ends would let a terminator in the same
            # chunk emit an over-long line untruncated.
            lines.append(self._overflow())

    def _at_cr(self, *, last: bool, lines: list[str]) -> None:
        """Handle a carriage return, holding a trailing one for the next chunk."""
        if last:
            # A trailing CR waits for the next chunk to say whether it is
            # CRLF. It is still a boundary if the stream ends.
            self._pending_cr = True
        else:
            self._boundary(lines)
            self._skip_lf = True

    def finish(self) -> tuple[str, ...]:
        """Return the final unterminated line, if one is retained.

        Returns
        -------
        tuple[str, ...]
            Zero or one line: the bounded trailing text, or nothing when the
            stream ended on a terminator or was discarding an overflow.
        """
        if self._discarding:
            return ()
        if self._pending_cr:
            self._pending_cr = False
            return (self._take(),)
        if not self._pending:
            return ()
        return (self._take(),)

    def _boundary(self, lines: list[str]) -> None:
        """Finish one line at a terminator, or drop a discarded remainder."""
        if self._discarding:
            self._discarding = False
            return
        if len(self._pending) > self.max_line_bytes:
            lines.append(self._overflow())
            self._discarding = False
            return
        lines.append(self._take())

    def _take(self) -> str:
        """Return the retained line and reset the partial buffer.

        A line of invalid UTF-8 expands when replacement characters are
        substituted, so the encoded result can exceed the byte budget the raw
        input respected. The decoded line is therefore re-checked here, which
        is the only place the newline, EOF, and truncation paths all pass.
        """
        text = self._decode(bytes(self._pending))
        self._pending.clear()
        self._discarding = False
        if len(text.encode("utf-8")) <= self.max_line_bytes:
            return text
        return self._truncate(text)

    def _truncate(self, text: str) -> str:
        """Cut `text` so the encoded line plus its marker fits the budget."""
        marker = self._marker()
        budget = max(0, self.max_line_bytes - len(marker.encode("utf-8")))
        while True:
            candidate = text.encode("utf-8")[:budget]
            decoded = candidate.decode("utf-8", errors="ignore")
            result = f"{decoded}{marker}"
            if len(result.encode("utf-8")) <= self.max_line_bytes or budget == 0:
                return result
            budget -= 1

    def _overflow(self) -> str:
        """Return a truncated prefix and start discarding the rest of the line.

        The marker and any UTF-8 replacement characters count against the same
        budget, so the encoded line the observer forwards never exceeds
        `max_line_bytes`.
        """
        raw = bytes(self._pending)
        self._pending.clear()
        self._discarding = True
        return self._truncate(self._decode(raw))

    def _decode(self, data: bytes) -> str:
        """Decode one complete line, flushing any incomplete sequence.

        A fresh decoder per line means an incomplete byte sequence cannot leak
        into the next line or be lost silently at end of stream: it becomes the
        replacement character at the boundary where it belongs.
        """
        return data.decode(self.encoding, errors=self.errors)
