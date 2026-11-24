"""Record live Kani facts and spool the report for the shared parser.

The observer is the supervision layer's streaming half. It reads the same
grammar the parser uses, so a banner, a selection record, and a verdict have
one definition, and it keeps only bounded facts on the heap. The log itself is
spooled to a temporary file and parsed once the child settles; a log beyond the
configured budget is reported as truncated evidence instead of being retained
without bound.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import tempfile
import typing as typ
from pathlib import Path

from rust_prover_tools.kani_log import (
    BANNER,
    COMPLETE,
    HARNESS_START,
    LIST_WRITTEN,
    NOISE_PREFIXES,
    VERIFICATION,
    KaniLog,
    KaniLogError,
    parse_kani_log_lines,
)
from rust_prover_tools.kani_log.syntax import NO_HARNESS_MATCH

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.models import OutputLine

__all__ = (
    "DEFAULT_MAX_LOG_BYTES",
    "KaniLiveFacts",
    "KaniObserver",
)

#: Bytes of one log the observer spools for the parser by default.
DEFAULT_MAX_LOG_BYTES: typ.Final[int] = 64 * 1024 * 1024

#: Live selection and verdict records retained before the facts fail closed.
MAX_LIVE_RECORDS: typ.Final[int] = 256


@dc.dataclass(frozen=True, slots=True)
class KaniLiveFacts:
    """The facts a live attempt has produced so far.

    Attributes
    ----------
    banner_version : str | None
        Release named by the opening banner, when one was seen.
    selection_records : tuple[str, ...]
        Harness names from ``Checking harness`` lines, in arrival order.
    verdicts : tuple[tuple[str, str], ...]
        ``(harness, verdict)`` pairs from ``VERIFICATION:`` lines.
    last_progress : str | None
        Last line that carried report structure, bounded by the caller.
    completed : bool
        Whether the log declared a completed run or an inventory.
    no_match_reason : str | None
        Kani's own "no harnesses matched" diagnostic, when it printed one.
    """

    banner_version: str | None
    selection_records: tuple[str, ...]
    verdicts: tuple[tuple[str, str], ...]
    last_progress: str | None
    completed: bool
    no_match_reason: str | None


@dc.dataclass(slots=True)
class KaniObserver:
    """Accumulate live Kani facts and spool the report for the shared parser.

    Attributes
    ----------
    max_log_bytes : int
        Largest log retained for the final parse. A longer log is reported as
        truncated evidence instead of being held without bound.
    banner_version : str | None
        Release named by the opening banner, when one was seen.
    selection_records : list[str]
        Harness names from ``Checking harness`` lines, in arrival order.
    verdicts : list[tuple[str, str]]
        ``(harness, verdict)`` pairs, in arrival order.
    last_progress : str | None
        Last line that carried report structure.
    completed : bool
        Whether the log declared a completed run or an inventory.
    no_match_reason : str | None
        Kani's own "no harnesses matched" diagnostic, when it printed one.
    failure_reason : str | None
        Why the shared parser refused the completed log, when it did.
    log_truncated : bool
        Whether the log exceeded `max_log_bytes`.
    facts_truncated : bool
        Whether the live record budget was exceeded. The attempt is then
        classified as an error rather than reported against partial facts.
    """

    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    banner_version: str | None = None
    selection_records: list[str] = dc.field(default_factory=list)
    verdicts: list[tuple[str, str]] = dc.field(default_factory=list)
    last_progress: str | None = None
    completed: bool = False
    no_match_reason: str | None = None
    failure_reason: str | None = None
    log_truncated: bool = False
    facts_truncated: bool = False
    _current: str | None = dc.field(default=None, init=False)
    _spool: typ.IO[str] | None = dc.field(default=None, init=False)
    _path: Path | None = dc.field(default=None, init=False)
    _stack: contextlib.ExitStack | None = dc.field(default=None, init=False)
    _written: int = dc.field(default=0, init=False)

    @property
    def facts(self) -> KaniLiveFacts:
        """The facts accumulated so far, without parsing the log."""
        return KaniLiveFacts(
            banner_version=self.banner_version,
            selection_records=tuple(self.selection_records),
            verdicts=tuple(self.verdicts),
            last_progress=self.last_progress,
            completed=self.completed,
            no_match_reason=self.no_match_reason,
        )

    def observe(self, line: OutputLine) -> None:
        """Record one decoded line's facts and spool it for the final parse."""
        text = line.text
        self._observe_facts(text)
        self._spool_line(text)

    def _observe_facts(self, text: str) -> None:
        """Update the live facts from one line."""
        if (match := BANNER.match(text)) is not None:
            self.banner_version = match["version"]
            return
        if (match := HARNESS_START.match(text)) is not None:
            self._current = match["name"]
            self._record(self.selection_records, match["name"])
            self.last_progress = text
            return
        if (match := VERIFICATION.match(text)) is not None:
            if self._current is not None:
                self._record(self.verdicts, (self._current, match["verdict"]))
            self.last_progress = text
            return
        if NO_HARNESS_MATCH.match(text) is not None:
            self.no_match_reason = text
            return
        if COMPLETE.match(text) is not None or text.startswith(LIST_WRITTEN):
            self.completed = True
            self.last_progress = text
            return
        if text.strip() and not text.startswith(NOISE_PREFIXES):
            self.last_progress = text

    def _record[T](self, records: list[T], value: T) -> None:
        """Retain one live record, failing closed when the budget is spent."""
        if len(records) >= MAX_LIVE_RECORDS:
            self.facts_truncated = True
            return
        records.append(value)

    def _spool_line(self, text: str) -> None:
        """Write one line to the spool while it fits the configured budget."""
        if self.log_truncated:
            return
        encoded = len(text.encode("utf-8")) + 1
        if self._written + encoded > self.max_log_bytes:
            self.log_truncated = True
            self._close_spool()
            return
        spool = self._spool if self._spool is not None else self._open_spool()
        spool.write(text)
        spool.write("\n")
        self._written += encoded

    def finish(self, exit_code: int) -> KaniLog | None:
        """Parse the spooled log with the shared parser, or record its refusal.

        Parameters
        ----------
        exit_code : int
            Raw child status, recorded by the parser in its facts.

        Returns
        -------
        KaniLog | None
            The parsed log, or `None` when the log was truncated or refused.
        """
        del exit_code
        if self.log_truncated:
            self.failure_reason = (
                f"the log exceeded the retained budget of {self.max_log_bytes} bytes"
            )
            return None
        try:
            return self._parse_spool()
        except KaniLogError as error:
            self.failure_reason = error.reason
            return None

    def _open_spool(self) -> typ.IO[str]:
        """Open and remember the spool the attempt keeps until it settles."""
        self._stack = contextlib.ExitStack()
        # pylint: disable-next=consider-using-with
        directory = self._stack.enter_context(tempfile.TemporaryDirectory())
        # The stack owns the handle; `finish` or the overflow path closes it.
        # pylint: disable-next=consider-using-with
        self._path = Path(directory) / "kani.log"
        handle = self._path.open("w+", encoding="utf-8", errors="replace")
        self._spool = self._stack.enter_context(handle)
        return self._spool

    def _parse_spool(self) -> KaniLog:
        """Parse the spooled log one line at a time, then release the spool."""
        if self._spool is None or self._path is None:
            msg = "the log does not begin with a Kani banner"
            raise KaniLogError(msg)
        try:
            self._spool.flush()
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                log = parse_kani_log_lines(line.removesuffix("\n") for line in handle)
        except BaseException:
            # The flush or parse failure is the primary one; a cleanup failure
            # while it unwinds must not replace it.
            with contextlib.suppress(Exception):
                self._close_spool()
            raise
        self._close_spool()
        return log

    def close(self) -> None:
        """Release the spool, tolerating a repeated or never-opened call.

        The spool is only opened when the child produces output, so a caller
        that classifies a stopped attempt without parsing still owns the
        handle. `close` is idempotent and safe on every classification path.
        """
        self._close_spool()

    def _close_spool(self) -> None:
        """Close the spool if one is open, tolerating a repeated call.

        The fields are cleared even when closing raises, so a cleanup failure
        cannot leave the observer holding a half-closed spool; the cleanup
        error itself propagates to the caller that observed it.
        """
        if self._stack is None:
            return
        stack, self._stack = self._stack, None
        self._spool = None
        self._path = None
        stack.close()
