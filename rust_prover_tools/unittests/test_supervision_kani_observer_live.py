"""Hold the live observer to its no-verdict and bounded-record contracts.

The observer never attributes a proof verdict, because Kani does not escape
description prose and a verdict-shaped line can be content. It also keeps only
a bounded number of selection records and reports when it had to drop one.
Both are properties of every possible line sequence, not of chosen examples.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.supervision import OutputLine
from rust_prover_tools.supervision.kani_observer import (
    MAX_LIVE_RECORDS,
    KaniObserver,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock

#: Lines that could look like a verdict, a selection, or description content.
_LINE = st.sampled_from((
    "Kani Rust Verifier 0.67.0 (cargo plugin)",
    "Checking harness harnesses::verified...",
    "VERIFICATION:- SUCCESSFUL",
    "VERIFICATION:- FAILED",
    '\t - Description: "quoted',
    'last"',
    "\t - Location: src/lib.rs:1:1 in function verified",
    "RESULTS:",
    "SUMMARY:",
    " ** 0 of 1 failed",
    "",
    "warning: build chatter",
))


def _observe(lines: list[str]) -> KaniObserver:
    """Feed one line sequence to a fresh observer."""
    observer = KaniObserver()
    clock = VirtualClock()
    for text in lines:
        observer.observe(
            OutputLine(
                stream="stdout",
                text=text,
                timestamp_utc=clock.now(),
                elapsed_seconds=0.0,
            )
        )
    return observer


@given(st.lists(_LINE, max_size=12))
@settings(max_examples=50, deadline=None)
def test_live_lines_never_supply_a_verdict(lines: list[str]) -> None:
    """No live sequence attributes a verdict, because prose can fake one."""
    observer = _observe(lines)

    assert observer.facts.verdicts == ()
    assert observer.verdicts == []


@given(
    st.lists(
        st.text(alphabet="abcdef", min_size=1, max_size=8),
        max_size=MAX_LIVE_RECORDS + 12,
    )
)
@settings(max_examples=10, deadline=None)
def test_retained_selection_records_stay_bounded(names: list[str]) -> None:
    """The live selection list never exceeds the budget and says when it did."""
    lines = [f"Checking harness {name}..." for name in names]

    observer = _observe(lines)

    assert len(observer.facts.selection_records) <= MAX_LIVE_RECORDS
    assert observer.facts_truncated == (len(names) > MAX_LIVE_RECORDS)
