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
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.manifest import Manifest


from rust_prover_tools.kani_log.syntax import (
    ASSERTION_KIND,
    COVER_KIND,
    UNSUPPORTED_KIND,
    UNWINDING_KINDS,
    PropertyResult,
)

__all__ = (
    "Observed",
    "denies_claim",
    "is_indeterminate",
    "lacks_panic_evidence",
    "panics_as_expected",
)

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
    def unresolved(self) -> bool:
        """Report whether any check was left undecided, wherever it is owned.

        Unlike :attr:`indeterminate`, this does not count an unreachable
        check.  It exists for the block whose failure a certified expected
        panic explains: the code after that panic is unreachable by
        construction, but a solver that returned no answer still proves
        nothing.
        """
        decided_everywhere = (*self.properties, *self.unattributed)
        return any(item.status == UNRESOLVED_STATUS for item in decided_everywhere)

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


def lacks_panic_evidence(seen: Observed, manifest: Manifest) -> bool:
    """Report whether a claim declared to panic lacks Kani's panic note."""
    return manifest.declares_panic(seen.name) and not seen.panics_as_expected


def is_indeterminate(seen: Observed, manifest: Manifest) -> bool:
    """Report whether a block leaves a claim undecided.

    A certified expected panic explains an unreachable check: the code after
    the panic is unreachable by construction, and Kani's note says the panic
    was the one the harness declared.  Counting that as undetermined would fail
    a claim the manifest asked for and Kani certified.  A solver that returned
    no answer is still undetermined, because the panic note says nothing about
    it.
    """
    if panics_as_expected(seen, manifest):
        return seen.unresolved
    return seen.indeterminate


def denies_claim(seen: Observed, manifest: Manifest) -> bool:
    """Report whether one block carries a failure its manifest cannot excuse."""
    failures = (*seen.substantive_failures, *seen.foreign_failures)
    if not failures:
        return False
    if not panics_as_expected(seen, manifest):
        return True
    return any(item.kind != ASSERTION_KIND for item in failures)


def panics_as_expected(seen: Observed, manifest: Manifest) -> bool:
    """Report whether the manifest declares this claim's panic, and it happened.

    The note is Kani's own certificate rather than an inference from the
    failure: the driver prints it only for a harness declared to panic whose
    every failed property is an assertion, and it is that combination the
    driver calls successful.  It therefore says nothing about a failure of any
    other kind, which is why the caller still reads the failed checks.
    """
    return seen.panics_as_expected and manifest.declares_panic(seen.name)
