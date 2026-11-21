"""State the properties a Kani analysis must satisfy over the fact product.

Each property is stated in the direction the specification requires: a claim
that keeps its assertion and its witness stands, a harness that does worse never
improves the outcome, and a failed claim assertion dominates everything below
it.

Every property other than "an undeclared harness is a structural error" is
stated over well-formed combinations only - those whose every harness the
manifest declares.  A role-less harness is a structural error, so a combination
containing one decides ``tool-error`` whatever else is true of it, and holding
an outcome claim to such a combination would test only the refusal.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import CLAIM, CONTROL
from tests import kani_log_combinations as combinations
from tests import kani_log_fixtures as facts

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log import KaniAnalysis

WITNESSES = ("satisfied", "unsatisfied")
CONTROL_DISPOSITIONS = (
    "rejected-as-intended",
    "rejected-otherwise",
    "accepted",
    "not-run",
)
STANDING = (facts.SUCCESS,)


def every_combination() -> cabc.Iterator[tuple[facts.HarnessFact, ...]]:
    """Yield every fact combination of one, two and three harnesses."""
    for depth in (1, 2, 3):
        yield from combinations.occurrences(depth)


def well_formed(combination: cabc.Sequence[facts.HarnessFact]) -> bool:
    """Report whether the manifest declares every harness in one combination."""
    return all(fact.role is not None for fact in combination)


def declared_combinations() -> cabc.Iterator[tuple[facts.HarnessFact, ...]]:
    """Yield the combinations the outcome properties are stated over."""
    for combination in every_combination():
        if well_formed(combination):
            yield combination


def claims_of(
    combination: cabc.Sequence[facts.HarnessFact],
) -> tuple[facts.HarnessFact, ...]:
    """Return the claim facts of one combination, in order."""
    return tuple(fact for fact in combination if fact.role == CLAIM)


def stands(fact: facts.HarnessFact) -> bool:
    """Report whether one claim fact leaves the claim proved.

    An unreachable witness denies it even when the assertion passed: a harness
    that assumed its own precondition away reports success having tested
    nothing.
    """
    excused = fact.status in STANDING or (
        fact.status == facts.FAILURE and fact.should_panic and fact.note
    )
    return excused and fact.cover != facts.UNREACHABLE


def claims_stand(combination: cabc.Sequence[facts.HarnessFact]) -> bool:
    """Report whether at least one claim stands and every claim stands.

    A combination of controls alone has no claim to verify, so it cannot be
    the thing ``verified`` describes; the empty ``all`` must not pass.
    """
    claims = claims_of(combination)
    return bool(claims) and all(stands(fact) for fact in claims)


def witness_of(fact: facts.HarnessFact) -> str:
    """Return the status one claim's declared cover must report."""
    return (
        "satisfied"
        if fact.status != facts.ABSENT and fact.cover == facts.SATISFIED
        else "unsatisfied"
    )


def declared_witnesses(
    combination: cabc.Sequence[facts.HarnessFact],
) -> tuple[str, ...]:
    """Return the witness statuses the manifest declares for one combination."""
    return tuple(
        witness_of(fact)
        for fact in claims_of(combination)
        if fact.cover != facts.NOTHING
    )


def controls_well_formed(
    combination: cabc.Sequence[facts.HarnessFact],
) -> bool:
    """Report whether one combination's controls report admitted dispositions."""
    decision = facts.decision_for(combination)
    controls = [fact for fact in combination if fact.role == CONTROL]
    return len(decision.control_statuses) == len(controls) and all(
        status in CONTROL_DISPOSITIONS for status in decision.control_statuses
    )


def degrade(
    combination: tuple[facts.HarnessFact, ...],
    index: int,
) -> tuple[facts.HarnessFact, ...]:
    """Replace one harness's behaviour with a worse one, keeping its identity."""
    fact = combination[index]
    worse = facts.TIMEOUT if fact.role == CONTROL else facts.FAILURE
    replaced = dc.replace(fact, status=worse)
    return (*combination[:index], replaced, *combination[index + 1 :])


def summarise(decision: KaniAnalysis) -> tuple[object, ...]:
    """Reduce one analysis to the facts the corpus records."""
    return (
        decision.outcome,
        decision.witness_statuses,
        decision.control_statuses,
        decision.run_mode,
    )


def names_of(combination: cabc.Sequence[facts.HarnessFact]) -> list[str]:
    """Name the harnesses of one combination, for a readable failure."""
    return [fact.name for fact in combination]
