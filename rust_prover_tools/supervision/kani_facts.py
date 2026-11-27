"""Accumulate Kani's live report facts without any I/O.

This is the pure half of the observation seam. It reads the same grammar the
shared parser uses, so a banner, a selection record, and a verdict have one
definition, and it keeps only bounded facts on the heap. An observer supplies
the lines; the scanner never opens, writes, or reads a file.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import (
    BANNER,
    COMPLETE,
    HARNESS_START,
    LIST_WRITTEN,
    NOISE_PREFIXES,
)
from rust_prover_tools.kani_log.syntax import CHECK, FIELD, NO_HARNESS_MATCH

__all__ = (
    "MAX_LIVE_RECORDS",
    "KaniFactScanner",
    "KaniLiveFacts",
)

#: Live selection and verdict records retained before the facts fail closed.
MAX_LIVE_RECORDS: typ.Final[int] = 256


@dc.dataclass(frozen=True, slots=True)
class _Candidate:
    """One report fact this scanner read, before it is known to be real."""

    kind: str
    value: str | None
    text: str


#: Report markers that can only follow a finished description.
_MARKERS: typ.Final[tuple[str, ...]] = (
    "RESULTS:",
    "SUMMARY:",
    "Manual Harness Summary:",
)


def _is_structural(text: str) -> bool:
    """Whether a line is a report structure that cannot be description content.

    Report-shaped candidates are excluded on purpose: a verdict, selection or
    banner printed inside a description looks exactly like the real one.
    """
    if FIELD.match(text) is not None:
        return True
    return text in _MARKERS or CHECK.match(text) is not None


def _is_notable(text: str) -> bool:
    """Whether a non-report line is worth remembering as progress."""
    return bool(text.strip()) and not text.startswith(NOISE_PREFIXES)


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
        Always empty in the live facts: a verdict is attributed only from the
        parsed log, because description prose can contain a verdict line.
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
class KaniFactScanner:
    """Accumulate live Kani facts from a line stream, without any I/O.

    Attributes
    ----------
    banner_version : str | None
        Release named by the opening banner, when one was seen.
    selection_records : list[str]
        Harness names from ``Checking harness`` lines, in arrival order.
    verdicts : list[tuple[str, str]]
        Never appended to: see the class note. Kept so ``facts`` has a stable
        shape for callers that read it.
    last_progress : str | None
        Last line that carried report structure.
    completed : bool
        Whether the log declared a completed run or an inventory.
    no_match_reason : str | None
        Kani's own "no harnesses matched" diagnostic, when it printed one.
    facts_truncated : bool
        Whether the live record budget was exceeded. The attempt is then
        classified as an error rather than reported against partial facts.
    """

    banner_version: str | None = None
    selection_records: list[str] = dc.field(default_factory=list)
    verdicts: list[tuple[str, str]] = dc.field(default_factory=list)
    last_progress: str | None = None
    completed: bool = False
    no_match_reason: str | None = None
    facts_truncated: bool = False
    _current: str | None = dc.field(default=None, init=False)
    _in_description: bool = dc.field(default=False, init=False)

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

    def observe(self, text: str) -> None:
        """Update the live facts from one line.

        Kani prints check descriptions without escaping newlines and escapes
        nothing inside them, so description content can contain a line that
        looks like a banner, a selection, a verdict, or the closing quote
        itself. The scanner therefore keeps the frame open from a
        ``Description`` field until a report structure that cannot be content
        follows - a field such as ``Location``, a results or summary marker, or
        a fresh check entry. Everything between is description content and is
        never recorded, which fails closed: an unresolved frame loses its live
        facts rather than crediting prose, and a complete log is parsed
        authoritatively.
        """
        if self._in_description:
            if not _is_structural(text):
                if _is_notable(text):
                    self.last_progress = text
                return
            self._in_description = False
        if self._opens_description(text):
            return
        candidate = self._candidate(text)
        if candidate is not None:
            self._commit(candidate)
            self.last_progress = text
        elif _is_notable(text):
            self.last_progress = text

    def _opens_description(self, text: str) -> bool:
        """Enter a description frame when a field value begins a quotation."""
        field = FIELD.match(text)
        if field is None or field["key"] != "Description":
            return False
        if not field["value"].startswith('"'):
            return False
        self._in_description = True
        return True

    @staticmethod
    def _candidate(text: str) -> _Candidate | None:
        """Return the fact `text` reports, or `None` for an ordinary line."""
        if (match := BANNER.match(text)) is not None:
            return _Candidate("banner", match["version"], text)
        if (match := HARNESS_START.match(text)) is not None:
            return _Candidate("selection", match["name"], text)
        if NO_HARNESS_MATCH.match(text) is not None:
            return _Candidate("no-match", None, text)
        if COMPLETE.match(text) is not None or text.startswith(LIST_WRITTEN):
            return _Candidate("complete", None, text)
        return None

    def _commit(self, candidate: _Candidate) -> None:
        """Record one fact this scanner read from a report line.

        A verdict is never attributed live. Kani escapes nothing inside a
        description, so a resolved line can still be prose; the attempt's
        parsed log supplies the verdict instead.
        """
        if candidate.kind == "banner":
            if self.banner_version is None:
                self.banner_version = candidate.value
        elif candidate.kind == "selection":
            self._current = candidate.value
            self._record(self.selection_records, typ.cast("str", candidate.value))
        elif candidate.kind == "no-match":
            self.no_match_reason = candidate.text
        elif candidate.kind == "complete":
            self.completed = True

    def _record[T](self, records: list[T], value: T) -> None:
        """Retain one live record, failing closed when the budget is spent."""
        if len(records) >= MAX_LIVE_RECORDS:
            self.facts_truncated = True
            return
        records.append(value)
