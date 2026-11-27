"""Record live Kani facts and spool the report for the shared parser.

The observation seam has two independent halves. `KaniFactScanner` is a pure
accumulator: it reads the same grammar the parser uses, so a banner, a
selection record, and a verdict have one definition, and it keeps only bounded
facts on the heap. `KaniObserver` adds the spool: it writes each line to a
temporary file and parses that file once the child settles. A log beyond the
configured budget is reported as truncated evidence instead of being retained
without bound. `KaniObservation` is the immutable value the classifier reads,
so classification neither parses the log nor owns the spool.

The pure accumulator and the spool live in `kani_facts` and `kani_spool`; they
are re-exported here so the observation seam has one import path.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import KaniLog, KaniLogError, parse_kani_log_lines
from rust_prover_tools.supervision.kani_facts import (
    MAX_LIVE_RECORDS,
    KaniFactScanner,
    KaniLiveFacts,
)
from rust_prover_tools.supervision.kani_spool import (
    KaniSpool,
    SpoolFactory,
    TemporaryKaniSpool,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.models import OutputLine

__all__ = (
    "DEFAULT_MAX_LOG_BYTES",
    "MAX_LIVE_RECORDS",
    "KaniFactScanner",
    "KaniLiveFacts",
    "KaniObservation",
    "KaniObserver",
    "KaniSpool",
    "SpoolFactory",
    "TemporaryKaniSpool",
)

#: Bytes of one log the observer spools for the parser by default.
DEFAULT_MAX_LOG_BYTES: typ.Final[int] = 64 * 1024 * 1024


@dc.dataclass(frozen=True, slots=True)
class KaniObservation:
    """The settled facts of one attempt, immutable for classification.

    Attributes
    ----------
    facts : KaniLiveFacts
        The live facts the scanner accumulated from the streamed lines.
    log : KaniLog | None
        The parsed report, or `None` when the log was truncated or refused.
    failure_reason : str | None
        Why the shared parser refused the completed log, when it did.
    log_truncated : bool
        Whether the log exceeded the observer's retained budget.
    facts_truncated : bool
        Whether the live record budget was exceeded.
    spool_error : str | None
        Why the spool could not be opened, when its factory failed.
    """

    facts: KaniLiveFacts
    log: KaniLog | None
    failure_reason: str | None = None
    log_truncated: bool = False
    facts_truncated: bool = False
    spool_error: str | None = None

    @property
    def banner_version(self) -> str | None:
        """Release named by the opening banner, delegated to the facts."""
        return self.facts.banner_version


@dc.dataclass(slots=True)
class KaniObserver:
    """Accumulate live Kani facts and spool the report for the shared parser.

    Attributes
    ----------
    max_log_bytes : int
        Largest log retained for the final parse. A longer log is reported as
        truncated evidence instead of being held without bound.
    spool_factory : SpoolFactory
        Opens the spool the attempt keeps until the supervisor closes it. An
        `OSError` from the factory is recorded as the observation's
        `spool_error` instead of ending the attempt.
    """

    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    spool_factory: SpoolFactory = TemporaryKaniSpool
    _scanner: KaniFactScanner = dc.field(default_factory=KaniFactScanner, init=False)
    _spool: KaniSpool | None = dc.field(default=None, init=False)
    _spool_error: str | None = dc.field(default=None, init=False)
    _log_truncated: bool = dc.field(default=False, init=False)
    _written: int = dc.field(default=0, init=False)
    _observation: KaniObservation | None = dc.field(default=None, init=False)

    def __post_init__(self) -> None:
        """Open the spool the attempt keeps until it settles."""
        try:
            self._spool = self.spool_factory()
        except OSError as error:
            # The run must still report what the verifier printed before the
            # spool failed, so the observation names the failure instead.
            self._spool_error = f"{type(error).__name__}: {error}"

    @property
    def facts(self) -> KaniLiveFacts:
        """The facts accumulated so far, without parsing the log."""
        return self._scanner.facts

    @property
    def banner_version(self) -> str | None:
        """Release named by the opening banner, when one was seen."""
        return self._scanner.banner_version

    @property
    def selection_records(self) -> list[str]:
        """Harness names from ``Checking harness`` lines, in arrival order."""
        return self._scanner.selection_records

    @property
    def verdicts(self) -> list[tuple[str, str]]:
        """Always empty: a verdict is attributed only from the parsed log."""
        return self._scanner.verdicts

    @property
    def last_progress(self) -> str | None:
        """Last line that carried report structure."""
        return self._scanner.last_progress

    @property
    def completed(self) -> bool:
        """Whether the log declared a completed run or an inventory."""
        return self._scanner.completed

    @property
    def no_match_reason(self) -> str | None:
        """Kani's own "no harnesses matched" diagnostic, when printed."""
        return self._scanner.no_match_reason

    @property
    def facts_truncated(self) -> bool:
        """Whether the live record budget was exceeded."""
        return self._scanner.facts_truncated

    @property
    def log_truncated(self) -> bool:
        """Whether the log exceeded `max_log_bytes`."""
        return self._log_truncated

    def observe(self, line: OutputLine) -> None:
        """Record one decoded line's facts and spool it for the final parse."""
        text = line.text
        self._scanner.observe(text)
        self._spool_line(text)

    def _spool_line(self, text: str) -> None:
        """Write one line to the open spool while it fits the configured budget.

        Once the budget is spent the spool can never be parsed, so writing
        stops; the live facts continue under their own cap. The spool itself
        stays open, because closing is the attempt boundary's job: a close
        failure must not escape the output callback.
        """
        if self._log_truncated or self._spool is None:
            return
        encoded = len(text.encode("utf-8")) + 1
        if self._written + encoded > self.max_log_bytes:
            self._log_truncated = True
            return
        self._spool.write(text)
        self._spool.write("\n")
        self._written += encoded

    def finish(self) -> KaniObservation:
        """Return the immutable observation, parsing the spool once at most.

        Returns
        -------
        KaniObservation
            The live facts, the parsed log when one was retained, and why it
            was not when the budget or the parser refused it. The spool stays
            open: the attempt boundary that owns the observer releases it.
        """
        if self._observation is None:
            self._observation = self._settle()
        return self._observation

    def _settle(self) -> KaniObservation:
        """Build the observation without closing or reopening the spool."""
        facts = self._scanner.facts
        if self._log_truncated:
            budget = self.max_log_bytes
            return self._observed(
                facts,
                failure_reason=(
                    f"the log exceeded the retained budget of {budget} bytes"
                ),
            )
        try:
            log = self._parse_spool()
        except KaniLogError as error:
            return self._observed(facts, failure_reason=error.reason)
        except OSError as error:
            # A flush or read failure is evidence of its own. Recording it in
            # the observation keeps `finish` infallible, so it can be called
            # while an attempt unwinds a cancellation or a monitor fault
            # without replacing that original failure.
            return self._observed(facts, spool_error=f"{type(error).__name__}: {error}")
        return self._observed(facts, log=log)

    def _observed(
        self,
        facts: KaniLiveFacts,
        *,
        log: KaniLog | None = None,
        failure_reason: str | None = None,
        spool_error: str | None = None,
    ) -> KaniObservation:
        """Compose the observation from the facts and the spool's outcome."""
        return KaniObservation(
            facts=facts,
            log=log,
            failure_reason=failure_reason,
            log_truncated=self._log_truncated,
            facts_truncated=self._scanner.facts_truncated,
            spool_error=spool_error if spool_error is not None else self._spool_error,
        )

    def _parse_spool(self) -> KaniLog:
        """Parse the spooled log one line at a time, without releasing it."""
        if self._spool is None:
            msg = "the log does not begin with a Kani banner"
            raise KaniLogError(msg)
        return parse_kani_log_lines(self._spool.lines())

    def close(self) -> None:
        """Release the spool, tolerating a repeated or never-opened call.

        The observation is unaffected: a caller may close the observer and
        still classify the value `finish` already returned. A cleanup failure
        propagates to the caller that observed it, which the attempt boundary
        suppresses so it cannot replace an active failure.
        """
        spool, self._spool = self._spool, None
        if spool is not None:
            spool.close()
