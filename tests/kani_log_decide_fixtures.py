"""Build the single blocks and logs the Kani decision cases are written around.

`kani_log_fixtures` enumerates per-harness fact classes for the product tests;
the cases in the ``test_kani_log_decide_*`` modules instead hand the analysis
one block and read the decision it produces.  Those cases differ in a check or
two, so the framing every real block carries - the echoed solver command, the
capture flags and a successful verdict - is built here once and overridden per
case.

Nothing here may be more generous than a real log: the framing is the shape the
parser requires, so a constructed block the extractor could not produce would
be refused rather than quietly accepted.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import (
    SUPPORTED_KANI_VERSIONS,
    FailedCheck,
    HarnessBlock,
    KaniLog,
    ManualSummary,
    PropertyResult,
)

__all__ = ("block", "log_of", "verified_log")


class _BlockFields(typ.TypedDict, total=False):
    """The per-case fields a test may override on the shared block framing."""

    failures: tuple[FailedCheck, ...]
    failed_count: int | None
    total_count: int | None
    covered_satisfied: int | None
    covered_total: int | None
    covered_unreachable: int | None
    verdict: str
    panics_as_expected: bool


def block(
    name: str,
    *checks: PropertyResult,
    **fields: typ.Unpack[_BlockFields],
) -> HarnessBlock:
    """Build a harness block with the framing the decision suites share.

    The echoed solver command, the capture flags and the successful verdict are
    the same in every case, so a caller passes only the checks and the fields
    its case is about.
    """
    framed = HarnessBlock(
        name=name,
        properties=checks,
        solver_command="[Kani] Running: `cbmc --sat-solver cadical`",
        seen_verification_time=True,
        has_results=True,
        verdict="SUCCESSFUL",
    )
    return dc.replace(framed, **fields)


def verified_log(harness_block: HarnessBlock) -> KaniLog:
    """Build a log whose one block is the run's one verified harness."""
    return KaniLog(
        next(iter(SUPPORTED_KANI_VERSIONS)),
        "regular",
        (harness_block,),
        ManualSummary(1, 0, 1, ()),
        (),
    )


def log_of(*names: str) -> KaniLog:
    """Build a log with one successful block per named harness."""
    return KaniLog(
        next(iter(SUPPORTED_KANI_VERSIONS)),
        "regular",
        tuple(
            block(
                name,
                PropertyResult(f"{name}.assertion.1", "SUCCESS", "the claim"),
                failed_count=0,
                total_count=1,
            )
            for name in names
        ),
        ManualSummary(len(names), 0, len(names), ()),
        (),
    )
