"""Judge whether each Kani negative control failed the way it was meant to.

A control is a harness declared to fail: a seeded mutation, a disabled check, a
broken lemma.  It demonstrates that verification notices the fault only when it
fails *the* fault the manifest names.  A control that passed proves nothing,
and one that failed somewhere else proves nothing either, so each declared
control is reported under one of three words rather than as a bare pass.

The judgement is a pure function of the manifest entry and the harness block
already parsed from the log, so it can be re-derived at any time.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.manifest import Harness, Manifest
    from rust_prover_tools.kani_log.observation import Observed
    from rust_prover_tools.kani_log.syntax import PropertyResult

__all__ = (
    "ACCEPTED",
    "NOT_RUN",
    "REJECTED_AS_INTENDED",
    "REJECTED_OTHERWISE",
    "control_statuses",
)

ACCEPTED = "accepted"
NOT_RUN = "not-run"
REJECTED_AS_INTENDED = "rejected-as-intended"
REJECTED_OTHERWISE = "rejected-otherwise"


def control_statuses(
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
        _matches_expected(item, expected, owned=item.owner == seen.name)
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
        names.add(item.name_in_owner)
    return expected in names
