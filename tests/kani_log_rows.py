"""Translate the decision table's rows into the facts the analysis reads.

The table is data, so the mapping from a row's JSON to a `HarnessFact` lives
beside the table's reader rather than in the fixture module that builds one
fact set.  Keeping the translation separate lets a row state an observed check
(`extra_status`) that no single-fact variant names.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log import KaniAnalysis

from tests.kani_log_fixtures import (
    AS_INTENDED,
    NOTHING,
    OTHERWISE,
    UNSUPPORTED_CHECK,
    HarnessFact,
    decision_for,
)

__all__ = ("decide_row", "facts_of_row")

TABLE_STATUSES = {"UNSUPPORTED_FAILURE": UNSUPPORTED_CHECK}
#: Every status a rendered check line can carry.  ``extra_status`` describes an
#: observed check, so a fact-class name is not one of these.
_CHECK_STATUSES = frozenset({"SUCCESS", "FAILURE", "UNDETERMINED", "UNREACHABLE"})


def _check_status(value: str) -> str:
    """Translate a row's ``extra_status`` into the status a check line carries.

    The value names a check's result, not a fact class.  Accepting a class name
    such as ``UNSUPPORTED_FAILURE`` would put it on the built check line, where
    no status like it exists and no rule matches it.
    """
    if value in _CHECK_STATUSES or value == NOTHING:
        return value
    msg = f"the decision-table extra_status {value!r} is not a check status"
    raise ValueError(msg)


def facts_of_row(row: dict[str, typ.Any]) -> tuple[HarnessFact, ...]:
    """Translate one decision-table row into the facts that describe it."""
    facts = []
    for entry in row["harnesses"]:
        covers = entry.get("covers") or ()
        if len(covers) > 1:
            msg = (
                f"the decision-table row for {entry['name']!r} declares "
                f"{len(covers)} covers, but the adapter represents one"
            )
            raise ValueError(msg)
        if covers and entry.get("cover_status", NOTHING) == NOTHING:
            # A declared cover with no status would be mapped to NOTHING,
            # which the manifest builder treats as no cover at all, so the
            # witness would silently disappear from the analysis.
            msg = (
                f"the decision-table row for {entry['name']!r} declares a "
                "cover without a cover_status"
            )
            raise ValueError(msg)
        facts.append(
            HarnessFact(
                entry["name"],
                entry.get("role"),
                TABLE_STATUSES.get(entry["status"], entry["status"]),
                entry.get("cover_status", NOTHING),
                AS_INTENDED
                if entry.get("expected_failure") == "assertion.1"
                else OTHERWISE,
                bool(entry.get("should_panic")),
                bool(entry.get("panics_as_expected")),
                cover_description=covers[0] if covers else None,
                expected_failure=entry.get("expected_failure"),
                extra=_check_status(entry.get("extra_status", NOTHING)),
            ),
        )
    return tuple(facts)


def decide_row(row: dict[str, typ.Any]) -> KaniAnalysis:
    """Decide one decision-table row."""
    return decision_for(
        facts_of_row(row),
        log_format=row.get("format", "regular"),
        unsupported_constructs=tuple(row.get("unsupported_constructs", ())),
    )
