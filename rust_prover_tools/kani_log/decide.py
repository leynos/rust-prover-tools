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

from rust_prover_tools.kani_log.extract import unwinding_checks_enabled
from rust_prover_tools.kani_log.observation import Observed
from rust_prover_tools.kani_log.syntax import (
    UNSUPPORTED_KIND,
    UNSUPPORTED_MARKER,
    KaniLogError,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.manifest import Harness, Manifest
    from rust_prover_tools.kani_log.model import HarnessBlock
    from rust_prover_tools.kani_log.model import KaniLog as ParsedLog
    from rust_prover_tools.kani_log.syntax import PropertyResult

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

FULL_PROFILE = "full-profile"
FOCUSED = "focused"
SATISFIED = "satisfied"
UNSATISFIED = "unsatisfied"
REJECTED_AS_INTENDED = "rejected-as-intended"
REJECTED_OTHERWISE = "rejected-otherwise"
ACCEPTED = "accepted"
NOT_RUN = "not-run"


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


def analyse_kani_log(log: ParsedLog, manifest: Manifest) -> KaniAnalysis:
    """Decide what a parsed log proves about a manifest's claims.

    Parameters
    ----------
    log : KaniLog
        Facts extracted from one Kani invocation.
    manifest : Manifest
        The harnesses the run was meant to cover.

    Returns
    -------
    KaniAnalysis
        The outcome, the per-cover and per-control statuses, the run mode and a
        short reason.  A structural mismatch becomes
        :attr:`KaniOutcome.TOOL_ERROR` rather than an exception.
    """
    try:
        return _analyse(log, manifest)
    except ValueError as error:
        return _tool_error(str(error))


def from_kani_log_error(error: KaniLogError) -> KaniAnalysis:
    """Return the analysis a structurally invalid log supports."""
    return _tool_error(error.reason)


def _tool_error(reason: str) -> KaniAnalysis:
    """Build the analysis for a log that could not be decided from."""
    return KaniAnalysis(KaniOutcome.TOOL_ERROR, reason=reason)


def _analyse(log: ParsedLog, manifest: Manifest) -> KaniAnalysis:
    """Apply the precedence table, refusing a structural mismatch."""
    if log.format != "regular":
        msg = f"the log is {log.format!r} output, which carries no harness verdicts"
        raise ValueError(msg)
    observed = _observations(log, manifest)
    claims = [observed.get(item.name) for item in manifest.claims]
    return KaniAnalysis(
        _outcome(log, claims, manifest),
        _witness_statuses(manifest, observed),
        _control_statuses(manifest, observed),
        _run_mode(log, manifest),
        reason="decided from the parsed log",
    )


def _observations(
    log: ParsedLog,
    manifest: Manifest,
) -> dict[str, Observed]:
    """Attribute every reported check to a manifest harness."""
    declared = set(manifest.names())
    observed: dict[str, Observed] = {}
    for block in log.blocks:
        if block.name not in declared:
            msg = (
                f"the log reports harness {block.name!r}, which the manifest "
                "does not declare"
            )
            raise ValueError(msg)
        owned = block.owned(block.name)
        observed[block.name] = Observed(
            name=block.name,
            succeeded=block.succeeded,
            properties=owned,
            # Covers stay here too: CBMC may print one without an owner, and
            # the block it appears in is still the harness it describes.
            unattributed=tuple(item for item in block.properties if item not in owned),
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
    from the block it appears in, which is the harness it was checked for.  Two
    covers may share a description, so every report for that description must
    be satisfied before the declared cover counts as satisfied.
    """
    statuses: list[str] = []
    for claim in manifest.claims:
        seen = observed.get(claim.name)
        reported: dict[str, list[str]] = {}
        for item in () if seen is None else (*seen.properties, *seen.unattributed):
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


def _control_statuses(
    manifest: Manifest,
    observed: dict[str, Observed],
) -> tuple[str, ...]:
    """Map each control to whether it was rejected, accepted or never ran."""
    statuses: list[str] = []
    for control in manifest.controls:
        seen = observed.get(control.name)
        if seen is None:
            statuses.append(NOT_RUN)
        elif seen.succeeded:
            statuses.append(ACCEPTED)
        elif _rejected_as_intended(control, seen):
            statuses.append(REJECTED_AS_INTENDED)
        else:
            statuses.append(REJECTED_OTHERWISE)
    return tuple(statuses)


def _rejected_as_intended(control: Harness, seen: Observed) -> bool:
    """Report whether a control failed exactly the property the manifest expects.

    The expected failure may name a full identifier, the property within the
    harness, or a check kind; anything else is ``rejected-otherwise``.  Every
    failed check must match, including one owned by a called routine: a seeded
    fault that also trips an unrelated check has not shown that the claim's own
    property would be caught.
    """
    expected = (control.expected_failure or "").strip()
    if not expected:
        return False
    failed = seen.failed_checks
    if not failed:
        return False
    return all(
        _matches_expected(item, expected, owned=item not in seen.unattributed)
        for item in failed
    )


def _matches_expected(
    item: PropertyResult,
    expected: str,
    *,
    owned: bool,
) -> bool:
    """Report whether one failed check is the failure the manifest expects.

    A property name such as ``assertion.1`` identifies a check only within its
    owner.  A called routine's check may carry the same property name, so only
    its full identifier or kind can name it.
    """
    names = {item.identifier, item.kind}
    if owned:
        names.add(item.property)
    return expected in names


def _outcome(
    log: ParsedLog,
    claims: list[Observed | None],
    manifest: Manifest,
) -> KaniOutcome:
    """Apply the precedence, strongest disproof first."""
    reported = [item for item in claims if item is not None]
    if _has_counterexample(reported, manifest):
        return KaniOutcome.COUNTEREXAMPLE
    if _has_resource_exhaustion(reported):
        return KaniOutcome.RESOURCE_EXHAUSTED
    if any(_is_unsupported(log, item) for item in reported):
        return KaniOutcome.UNSUPPORTED
    if any(item.indeterminate for item in reported):
        return KaniOutcome.UNDETERMINED
    if len(reported) != len(manifest.claims):
        return KaniOutcome.SKIPPED
    return _standing_outcome(reported)


def _has_counterexample(
    reported: list[Observed],
    manifest: Manifest,
) -> bool:
    """Report whether a claim was disproved by its own or a called failure."""
    return any(
        _counts_as_counterexample(item, manifest) or bool(item.foreign_failures)
        for item in reported
    )


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


def _standing_outcome(reported: list[Observed]) -> KaniOutcome:
    """Return the outcome left when nothing above applies.

    A block Kani still declared failed had a failure somewhere.  If none of
    the attributed checks carries it, reading the verdict stops the run from
    being called verified by default.
    """
    if any(not item.succeeded for item in reported):
        return KaniOutcome.COUNTEREXAMPLE
    return KaniOutcome.VERIFIED


def _counts_as_counterexample(seen: Observed, manifest: Manifest) -> bool:
    """Report whether this claim's failed property disproves the claim."""
    if not seen.substantive_failures:
        return False
    entry = next(item for item in manifest.claims if item.name == seen.name)
    return not (entry.should_panic and seen.panics_as_expected)


def _is_unsupported(log: ParsedLog, seen: Observed) -> bool:
    """Report whether Kani could not model a construct this harness reached.

    Three shapes say so.  A failed ``unsupported_construct`` check is
    unambiguous.  A failure whose message names the unsupported marker is the
    shape the corpus records.  The pre-flight warning alone proves nothing - it
    covers the whole crate - so it counts only when this harness was left
    undecided.
    """
    if any(
        item.kind == UNSUPPORTED_KIND and item.status == "FAILURE"
        for item in seen.properties
    ):
        return True
    if any(UNSUPPORTED_MARKER in text.lower() for text in seen.failure_messages):
        return True
    return bool(log.unsupported_constructs) and seen.indeterminate


def _run_mode(log: ParsedLog, manifest: Manifest) -> str:
    """Report ``full-profile`` only when nothing could have been left out.

    Coverage is a set comparison: Kani emits blocks in its own order, so
    comparing positions would report a complete run as focused merely because
    the manifest listed its harnesses differently.
    """
    observed = log.harnesses
    claimed = manifest.names()
    if len(set(observed)) != len(observed) or set(observed) != set(claimed):
        return FOCUSED
    if not all(_unwinding_enabled(block) for block in log.blocks):
        return FOCUSED
    return FULL_PROFILE


def _unwinding_enabled(block: HarnessBlock) -> bool:
    """Read the tri-state unwinding check as a yes/no gate."""
    return unwinding_checks_enabled(block) is True
