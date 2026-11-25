"""Presentation of one validated Kani proof report.

Rendering is a separate reason to change from parsing: a snapshot fixture pins
this text, while the parser's contract is about what a transcript means. The
two therefore live apart, and this module imports the report rather than the
reverse.

The output is deliberately narrow — sorted harness names and integer counts —
so a captured transcript's timestamps, versions and paths never reach it.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.producer.proof_report import ProofReport

__all__ = ("render_proof_report",)


def render_proof_report(report: ProofReport) -> str:
    """Render a report in a stable form suitable for a snapshot.

    Parameters
    ----------
    report : ProofReport
        Validated report.

    Returns
    -------
    str
        One line per category, with names sorted.
    """
    verified = ", ".join(sorted(report.verified_names)) or "-"
    failed = ", ".join(sorted(report.failed_names)) or "-"
    return (
        f"verified: {verified}\n"
        f"failed: {failed}\n"
        f"counts: {report.verified_count} verified, {report.failed_count} failed, "
        f"{report.total_count} total"
    )
