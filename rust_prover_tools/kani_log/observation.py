"""Reduce one parsed harness block to the facts the decision reads.

A block records everything Kani printed; the decision needs a narrower set:
whether the harness succeeded, which checks it owns, which failures belong to a
called routine, and whether any check was left undecided.  Deriving those once,
here, keeps the precedence table in :mod:`.decide` a comparison of named
properties rather than a second pass over raw output.

The class is frozen and holds tuples only, so an observation can be compared
and hashed like the block it came from.
"""

from __future__ import annotations

import dataclasses as dc

from rust_prover_tools.kani_log.syntax import (
    COVER_KIND,
    UNSUPPORTED_KIND,
    UNWINDING_KIND,
    PropertyResult,
)

__all__ = ("Observed",)

INDETERMINATE_STATUSES = frozenset({"UNDETERMINED", "UNREACHABLE"})
#: Check kinds whose failure limits or annotates a run rather than denying it.
_NON_DENYING_KINDS = frozenset({COVER_KIND, UNWINDING_KIND, UNSUPPORTED_KIND})


@dc.dataclass(frozen=True, slots=True)
class Observed:
    """What one log block says about a harness, reduced to the facts read here."""

    name: str
    succeeded: bool
    properties: tuple[PropertyResult, ...] = ()
    unattributed: tuple[PropertyResult, ...] = ()
    failure_messages: tuple[str, ...] = ()
    timed_out: bool = False
    panics_as_expected: bool = False

    @property
    def failures(self) -> tuple[PropertyResult, ...]:
        """The block's checks that failed."""
        return tuple(item for item in self.properties if item.status == "FAILURE")

    @property
    def unwinding_failures(self) -> tuple[PropertyResult, ...]:
        """The block's failed unwinding assertions."""
        return tuple(item for item in self.failures if item.is_unwinding)

    @property
    def substantive_failures(self) -> tuple[PropertyResult, ...]:
        """Failures that deny the claim rather than limit the run.

        An unwinding failure says the bound was too small, not that the
        property is false.  An unsupported-construct failure says Kani could
        not model the code.  Counting either here would turn a bounded or
        unsupported run into a counterexample.
        """
        return tuple(
            item for item in self.failures if item.kind not in _NON_DENYING_KINDS
        )

    @property
    def failed_checks(self) -> tuple[PropertyResult, ...]:
        """Every check the block reports as failed, whoever owns it.

        A control that is expected to trip one property has shown nothing if it
        also trips another, so the expected-failure decision reads this whole
        set rather than the harness's own failures alone.
        """
        return tuple(
            item
            for item in (*self.properties, *self.unattributed)
            if item.status == "FAILURE"
        )

    @property
    def foreign_failures(self) -> tuple[PropertyResult, ...]:
        """The block's substantive failures owned by a called routine."""
        return tuple(
            item
            for item in self.unattributed
            if item.status == "FAILURE" and item.kind not in _NON_DENYING_KINDS
        )

    @property
    def foreign_unwinding_failures(self) -> tuple[PropertyResult, ...]:
        """The block's unwinding failures owned by a called routine."""
        return tuple(
            item
            for item in self.unattributed
            if item.status == "FAILURE" and item.is_unwinding
        )

    @property
    def indeterminate(self) -> bool:
        """Report whether any of the harness's own checks was left undecided.

        An unreachable cover counts.  A harness that assumes its precondition
        away reports a successful verdict and no failed checks; the unreachable
        witness is the only evidence that nothing was proved.
        """
        return any(item.status in INDETERMINATE_STATUSES for item in self.properties)
