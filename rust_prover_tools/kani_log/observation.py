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
    UNWINDING_KINDS,
    PropertyResult,
)

__all__ = ("Observed",)

#: A check the solver could not decide, wherever in the call tree it sits.
UNRESOLVED_STATUS = "UNDETERMINED"
#: A check the harness never reaches, which only its own owner can explain.
UNREACHABLE_STATUS = "UNREACHABLE"
#: Check kinds whose failure limits or annotates a run rather than denying it.
#: The unwinding set is shared with :attr:`PropertyResult.is_unwinding`, so a
#: recursion bound is classified the same way everywhere it matters.
_NON_DENYING_KINDS = frozenset({COVER_KIND, UNSUPPORTED_KIND}) | UNWINDING_KINDS


@dc.dataclass(frozen=True, slots=True)
class Observed:
    """What one log block says about a harness, reduced to the facts read here."""

    name: str
    succeeded: bool
    failed_verdict: bool = False
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
    def witnessed(self) -> tuple[PropertyResult, ...]:
        """Every check that may witness this harness's declared covers.

        The harness's own checks count, and so does a cover CBMC printed
        without an owner: the block it appears in is the harness it was
        checked for.  A check attributed to a called routine is that routine's
        result, and two harnesses may describe their witnesses with the same
        words, so a foreign cover cannot stand in for this harness's declared
        one.
        """
        ownerless = (item for item in self.unattributed if item.owner is None)
        return (*self.properties, *ownerless)

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
        """Report whether any check in the block was left undecided.

        An unresolved check counts wherever it is owned.  The solver returned
        no answer for an assertion the verdict rests on, and the routine that
        owns it is part of what the harness claims, so the block proves nothing
        even when the assertion sits in a called routine.

        An unreachable check counts only when this harness owns it or CBMC
        printed it without an owner.  A check attributed to another routine is
        that routine's, and its unreachability says nothing about this harness,
        which may simply never take that path.  A harness that assumes its
        precondition away reports a successful verdict and no failed checks;
        the unreachable witness is the only evidence that nothing was proved.
        """
        ownerless = (item for item in self.unattributed if item.owner is None)
        decided_everywhere = (*self.properties, *self.unattributed)
        if any(item.status == UNRESOLVED_STATUS for item in decided_everywhere):
            return True
        return any(
            item.status == UNREACHABLE_STATUS for item in (*self.properties, *ownerless)
        )
