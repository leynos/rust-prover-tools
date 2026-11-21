"""Decide what a parsed Kani log proves against its harness manifest.

The parser says what the log contains; this module says what that means.  The
decision is a pure function of the parsed log and the manifest, so it can be
re-derived from those two inputs at any time and the tests can enumerate every
combination of per-harness facts instead of sampling a few runs.

The manifest supplies what a log cannot state about itself: which harnesses are
claims and which are negative controls, what property each control is supposed
to fail, and which claim panics on purpose.  It is also what turns a claim
missing from the log into :attr:`KaniOutcome.SKIPPED`; a log cannot show what it
omits.

Outcomes are ordered by how much the log disproved.  A counterexample outweighs
resource exhaustion, which outweighs an unsupported construct, which outweighs
an undecided check, which outweighs a harness that never ran.  A Kani result is
always bounded - the tool proves a property within its unwinding bounds, not
over every execution - so no field here claims otherwise.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from rust_prover_tools.kani_log.controls import (
    ACCEPTED,
    NOT_RUN,
    REJECTED_AS_INTENDED,
    REJECTED_OTHERWISE,
    control_statuses,
)
from rust_prover_tools.kani_log.observation import Observed
from rust_prover_tools.kani_log.run_mode import FOCUSED, FULL_PROFILE, run_mode
from rust_prover_tools.kani_log.syntax import (
    ASSERTION_KIND,
    UNSUPPORTED_KIND,
    UNSUPPORTED_MARKER,
    KaniLogError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log.manifest import Manifest
    from rust_prover_tools.kani_log.model import KaniLog as ParsedLog

__all__ = (
    "ACCEPTED",
    "FOCUSED",
    "FULL_PROFILE",
    "NOT_RUN",
    "REJECTED_AS_INTENDED",
    "REJECTED_OTHERWISE",
    "SATISFIED",
    "UNSATISFIED",
    "KaniAnalysis",
    "KaniOutcome",
    "analyse_kani_log",
    "from_kani_log_error",
)

SATISFIED = "satisfied"
UNSATISFIED = "unsatisfied"


class KaniOutcome(enum.StrEnum):
    """The strongest fact a Kani log supports about the declared claims."""

    VERIFIED = "verified"
    COUNTEREXAMPLE = "counterexample"
    RESOURCE_EXHAUSTED = "resource-exhausted"
    UNSUPPORTED = "unsupported"
    UNDETERMINED = "undetermined"
    SKIPPED = "skipped"
    TOOL_ERROR = "tool-error"


@dc.dataclass(frozen=True, slots=True)
class KaniAnalysis:
    """The outcome of one manifest-bound decision over a Kani log."""

    outcome: KaniOutcome
    witness_statuses: tuple[str, ...] = ()
    control_statuses: tuple[str, ...] = ()
    run_mode: str = FOCUSED
    reason: str = ""

    @property
    def passed(self) -> bool:
        """Report whether the claims and their guards all held.

        A run passes only when Kani verified the claims, every declared cover
        was satisfied and every control was rejected as intended.  A skipped
        claim, an unreachable cover or a control that failed for the wrong
        reason all leave the run formally completed but not passing.
        """
        return (
            self.outcome is KaniOutcome.VERIFIED
            and all(status == SATISFIED for status in self.witness_statuses)
            and all(status == REJECTED_AS_INTENDED for status in self.control_statuses)
        )


def analyse_kani_log(
    log: ParsedLog,
    manifest: Manifest,
    *,
    declared: cabc.Iterable[str] | None = None,
) -> KaniAnalysis:
    """Decide what a parsed log proves about a manifest's claims.

    Parameters
    ----------
    log : KaniLog
        Facts extracted from one Kani invocation.
    manifest : Manifest
        The harnesses the run was meant to cover.
    declared : Iterable[str] | None
        Every harness name the manifest declared before it was narrowed.  A
        ``--harness`` selection leaves the log able to report a harness that
        was declared but not selected, and that block is skipped rather than
        refused.  Defaults to the manifest's own names.

    Returns
    -------
    KaniAnalysis
        The outcome, the per-cover and per-control statuses, the run mode and a
        short reason.  A structural mismatch becomes
        :attr:`KaniOutcome.TOOL_ERROR` rather than an exception.
    """
    try:
        return _analyse(log, manifest, declared)
    except ValueError as error:
        return _tool_error(str(error))


def from_kani_log_error(error: KaniLogError) -> KaniAnalysis:
    """Return the analysis a structurally invalid log supports."""
    return _tool_error(error.reason)


def _tool_error(reason: str) -> KaniAnalysis:
    """Build the analysis for a log that could not be decided from."""
    return KaniAnalysis(KaniOutcome.TOOL_ERROR, reason=reason)


def _analyse(
    log: ParsedLog,
    manifest: Manifest,
    declared: cabc.Iterable[str] | None,
) -> KaniAnalysis:
    """Apply the precedence table, refusing a structural mismatch."""
    if log.format != "regular":
        msg = f"the log is {log.format!r} output, which carries no harness verdicts"
        raise ValueError(msg)
    observed = _observations(log, manifest, declared)
    claims = [observed.get(item.name) for item in manifest.claims]
    return KaniAnalysis(
        _outcome(log, claims, manifest),
        _witness_statuses(manifest, observed),
        control_statuses(manifest, observed),
        run_mode(log, manifest),
        reason="decided from the parsed log",
    )


def _observations(
    log: ParsedLog,
    manifest: Manifest,
    declared: cabc.Iterable[str] | None,
) -> dict[str, Observed]:
    """Attribute every reported check to a harness the run declares.

    A manifest narrowed by ``--harness`` lists only the selected harnesses, so
    a log captured from a wider run may still hold blocks for declared ones the
    caller did not select.  Those blocks are skipped: the selection is what the
    call asks about.  A block no declaration names remains a structural
    mismatch, because the log and the manifest then describe different runs.
    """
    known = frozenset(manifest.names() if declared is None else declared)
    selected = frozenset(manifest.names())
    observed: dict[str, Observed] = {}
    for block in log.blocks:
        if block.name not in known:
            msg = (
                f"the log reports harness {block.name!r}, which the manifest "
                "does not declare"
            )
            raise ValueError(msg)
        if block.name not in selected:
            continue
        observed[block.name] = Observed(
            name=block.name,
            succeeded=block.succeeded,
            failed_verdict=block.verdict == "FAILED",
            properties=block.owned(block.name),
            # Covers stay here too: CBMC may print one without an owner, and
            # the block it appears in is still the harness it describes.  The
            # owner test is the complement of ``owned`` without a quadratic
            # scan of that tuple.
            unattributed=tuple(
                item for item in block.properties if item.owner != block.name
            ),
            failure_messages=tuple(item.message for item in block.failures),
            timed_out=block.timed_out,
            panics_as_expected=block.panics_as_expected,
        )
    return observed


def _witness_statuses(
    manifest: Manifest,
    observed: dict[str, Observed],
) -> tuple[str, ...]:
    """Map each declared cover to what Kani reported, scoped to its claim.

    Two harnesses may describe their witnesses with the same words, so a
    global lookup would let one claim's satisfied cover stand in for another
    claim's unreachable one.  A cover CBMC printed without an owner is read
    from the block it appears in, which is the harness it was checked for; one
    owned by a called routine is that routine's result and is not this claim's
    witness.  Two covers may share a description, so every report for that
    description must be satisfied before the declared cover counts as
    satisfied.
    """
    statuses: list[str] = []
    for claim in manifest.claims:
        seen = observed.get(claim.name)
        reported: dict[str, list[str]] = {}
        for item in () if seen is None else seen.witnessed:
            if item.is_cover:
                reported.setdefault(item.description, []).append(item.status)
        statuses.extend(
            SATISFIED if _all_satisfied(reported.get(cover)) else UNSATISFIED
            for cover in claim.covers
        )
    return tuple(statuses)


def _all_satisfied(statuses: list[str] | None) -> bool:
    """Report whether one description was reported satisfied wherever it ran."""
    return bool(statuses) and all(status == "SATISFIED" for status in statuses)


def _outcome(
    log: ParsedLog,
    claims: list[Observed | None],
    manifest: Manifest,
) -> KaniOutcome:
    """Apply the precedence, strongest disproof first."""
    reported = [item for item in claims if item is not None]
    if _has_counterexample(reported, manifest):
        return KaniOutcome.COUNTEREXAMPLE
    # A block Kani still declared failed had a failure somewhere.  If none of
    # the attributed checks carries it, reading the verdict stops the run from
    # being called verified by default.  A disproof outranks exhaustion, so
    # this is decided first; a timeout's own exhaustion is not an unexplained
    # failure and keeps its place below.
    if any(_unexplained_failure(item) for item in reported):
        return KaniOutcome.COUNTEREXAMPLE
    if _has_resource_exhaustion(reported):
        return KaniOutcome.RESOURCE_EXHAUSTED
    if any(_is_unsupported(log, item, manifest) for item in reported):
        return KaniOutcome.UNSUPPORTED
    if any(_is_indeterminate(item, manifest) for item in reported):
        return KaniOutcome.UNDETERMINED
    return _final_outcome(reported, manifest)


def _final_outcome(
    reported: list[Observed],
    manifest: Manifest,
) -> KaniOutcome:
    """Decide between verified and skipped once no stronger fact applies.

    Controls are guards, not claims.  A manifest declaring only controls asks
    the run to prove nothing, and an empty ``all()`` over the claims must not be
    read as success, so it is skipped rather than verified.
    """
    if not manifest.claims:
        return KaniOutcome.SKIPPED
    return (
        KaniOutcome.VERIFIED
        if len(reported) == len(manifest.claims)
        else KaniOutcome.SKIPPED
    )


def _is_indeterminate(seen: Observed, manifest: Manifest) -> bool:
    """Report whether a block leaves a claim undecided.

    A certified expected panic explains an unreachable check: the code after
    the panic is unreachable by construction, and Kani's note says the panic
    was the one the harness declared.  Counting that as undetermined would fail
    a claim the manifest asked for and Kani certified.  A solver that returned
    no answer is still undetermined, because the panic note says nothing about
    it.
    """
    if _panics_as_expected(seen, manifest):
        return seen.unresolved
    return seen.indeterminate


def _unexplained_failure(seen: Observed) -> bool:
    """Report whether Kani failed a block that no reported check explains.

    A failed verdict says something failed.  When every check the block
    attributes passed, or no check was reported at all, the failure sits
    somewhere the analysis cannot see, and treating the run as verified would
    accept an unproved claim.  A timeout is excluded because its exhaustion is
    the explanation, and it is classified as resource exhaustion below.  A
    block with any failed check is excluded because the reported checks
    already carry that failure, whatever its kind.
    """
    return seen.failed_verdict and not seen.timed_out and not seen.failed_checks


def _has_counterexample(
    reported: list[Observed],
    manifest: Manifest,
) -> bool:
    """Report whether a claim was disproved by its own or a called failure.

    The expected-panic exemption covers both sources, but only the checks Kani
    counts as panics.  A panic aborts the routine it happens in and unwinds
    through the harness, so an assertion that fails beneath a claim declared to
    panic is that same panic witnessed one frame down.  A failure of any other
    kind is not: the note cannot explain it, and excusing it would report the
    claim verified beside an independent safety failure.
    """
    return any(_denies_claim(seen, manifest) for seen in reported)


def _denies_claim(seen: Observed, manifest: Manifest) -> bool:
    """Report whether one block carries a failure its manifest cannot excuse."""
    failures = (*seen.substantive_failures, *seen.foreign_failures)
    if not failures:
        return False
    if not _panics_as_expected(seen, manifest):
        return True
    return any(item.kind != ASSERTION_KIND for item in failures)


def _has_resource_exhaustion(reported: list[Observed]) -> bool:
    """Report whether any claim timed out or ran out of unwinding bound."""
    return any(
        any((
            item.timed_out,
            item.unwinding_failures,
            item.foreign_unwinding_failures,
        ))
        for item in reported
    )


def _panics_as_expected(seen: Observed, manifest: Manifest) -> bool:
    """Report whether the manifest declares this claim's panic, and it happened.

    The note is Kani's own certificate rather than an inference from the
    failure: the driver prints it only for a harness declared to panic whose
    every failed property is an assertion, and it is that combination the
    driver calls successful.  It therefore says nothing about a failure of any
    other kind, which is why the caller still reads the failed checks.
    """
    if not seen.panics_as_expected:
        return False
    entry = next(item for item in manifest.claims if item.name == seen.name)
    return entry.should_panic


def _is_unsupported(
    log: ParsedLog,
    seen: Observed,
    manifest: Manifest,
) -> bool:
    """Report whether Kani could not model a construct this harness reached.

    Three shapes say so.  A failed ``unsupported_construct`` check is
    unambiguous, whoever owns it: a check a called routine could not model is
    the harness's problem too, because the harness's verdict rests on that
    routine.  A failure whose message names the unsupported marker is the
    shape the corpus records.  The pre-flight warning alone proves nothing - it
    covers the whole crate - so it counts only when this harness was left
    undecided, with the same expected-panic exemption the outcome applies: an
    unreachable check beside a certified panic is explained, not undecided.
    """
    if any(
        item.kind == UNSUPPORTED_KIND and item.status == "FAILURE"
        for item in (*seen.properties, *seen.unattributed)
    ):
        return True
    if any(UNSUPPORTED_MARKER in text.lower() for text in seen.failure_messages):
        return True
    return bool(log.unsupported_constructs) and _is_indeterminate(seen, manifest)
