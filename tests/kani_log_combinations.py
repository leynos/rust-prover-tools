"""Enumerate the per-harness fact combinations the analysis is checked over.

This module owns the input space, not a record of one log.  Every claim
outcome, every witness state and every control disposition is a variant, and a
combination is one ordered choice of variants up to three harnesses deep.
:mod:`tests.kani_log_fixtures` turns one combination into the log and manifest
it describes; this module decides which combinations exist.

Keeping the enumeration apart from the builder is what keeps each module small
enough to read in one sitting, and it makes the input space itself reviewable:
a variant added here widens every property stated over it.
"""

from __future__ import annotations

import dataclasses as dc
import itertools
import typing as typ

from rust_prover_tools.kani_log import CLAIM, CONTROL
from tests.kani_log_fixtures import (
    ABSENT,
    AS_INTENDED,
    COVER_STATUSES,
    FAILURE,
    FOREIGN_FAILURE,
    FOREIGN_UNWIND_FAILURE,
    NOTHING,
    OTHERWISE,
    SATISFIED,
    SUCCESS,
    TIMEOUT,
    UNATTRIBUTED_FAILURE,
    UNDETERMINED,
    UNEXPLAINED_FAILURE,
    UNSUPPORTED_ASSERTION,
    UNSUPPORTED_CHECK,
    UNWIND_FAILURE,
    HarnessFact,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "claim_variants",
    "control_variants",
    "occurrences",
    "undeclared_variant",
    "variants",
)


def claim_variants(name: str = "h::c") -> tuple[HarnessFact, ...]:
    """Return one fact per decision-relevant class of claim harness."""
    outcomes = tuple(
        HarnessFact(name, CLAIM, status)
        for status in (
            SUCCESS,
            FAILURE,
            FOREIGN_FAILURE,
            FOREIGN_UNWIND_FAILURE,
            UNATTRIBUTED_FAILURE,
            UNEXPLAINED_FAILURE,
            TIMEOUT,
            UNDETERMINED,
            UNWIND_FAILURE,
            UNSUPPORTED_CHECK,
            UNSUPPORTED_ASSERTION,
        )
    )
    witnesses = tuple(
        HarnessFact(name, CLAIM, SUCCESS, cover) for cover in COVER_STATUSES[1:]
    )
    panics = tuple(
        HarnessFact(name, CLAIM, FAILURE, should_panic=True, note=note)
        for note in (True, False)
    )
    absent = tuple(
        HarnessFact(name, CLAIM, ABSENT, cover) for cover in (NOTHING, SATISFIED)
    )
    return outcomes + witnesses + panics + absent


def control_variants(name: str = "h::k") -> tuple[HarnessFact, ...]:
    """Return one fact per decision-relevant class of negative control."""
    return tuple(
        HarnessFact(name, CONTROL, status)
        for status in (
            SUCCESS,
            AS_INTENDED,
            OTHERWISE,
            TIMEOUT,
            UNDETERMINED,
            ABSENT,
            UNWIND_FAILURE,
        )
    )


def undeclared_variant(name: str = "h::u") -> HarnessFact:
    """Return the harness a log reports but no manifest declares."""
    return HarnessFact(name, None, SUCCESS)


def variants() -> tuple[HarnessFact, ...]:
    """Return every per-harness fact set the enumeration draws from."""
    return claim_variants() + control_variants() + (undeclared_variant(),)


def occurrences(depth: int) -> cabc.Iterator[tuple[HarnessFact, ...]]:
    """Yield every ordered fact combination of ``depth`` harnesses."""
    for facts in itertools.product(variants(), repeat=depth):
        yield tuple(
            dc.replace(fact, name=f"h::{index}{fact.name.removeprefix('h::')}")
            for index, fact in enumerate(facts)
        )
