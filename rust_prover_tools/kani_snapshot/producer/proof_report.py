"""Strict readings of one Kani proof transcript.

A non-zero process status is not a proof result: a compile error, a solver
crash, a run that discovered no harnesses and a harness that genuinely failed
all look alike at the shell. The producer therefore reads the three statements
the pinned Kani driver writes — the named `Checking harness` start, that
harness's own `VERIFICATION:-` verdict and the final manual summary — and
requires them to agree.

The parser is pure: it takes text and returns frozen records. Bounding the
input belongs here too, because a consumer may hand it a whole captured log.
"""

from __future__ import annotations

import dataclasses as dc
import itertools
import re
import typing as typ

from rust_prover_tools.kani_snapshot.producer.proof_transcript import (
    HarnessOutcome,
    ProofCompileError,
    ProofSolverError,
    ProofTranscriptError,
    Summary,
    harness_outcome,
    harness_sections,
    has_no_harness_message,
    summary_counts,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "MAX_TRANSCRIPT_BYTES",
    "HarnessOutcome",
    "ProofCompileError",
    "ProofReport",
    "ProofSolverError",
    "ProofTranscriptError",
    "parse_proof_report",
    "render_proof_report",
    "require_named_harnesses",
)

#: Largest transcript accepted. The CLI retains a bounded tail per stream, so a
#: larger value would only be a log a caller assembled itself.
MAX_TRANSCRIPT_BYTES: typ.Final[int] = 4 * 1024 * 1024

#: How to describe a harness that carried the status opposite the expectation.
_OPPOSITE_STATUS: typ.Final[dict[str, str]] = {
    "verify": "failed but was expected to verify",
    "fail": "verified but was expected to fail",
}

#: A compiler diagnostic, as opposed to a verification verdict.
_COMPILE_ERROR: typ.Final[re.Pattern[str]] = re.compile(
    r"^error(?:\[[A-Za-z0-9]+\])?: ", re.MULTILINE
)
_CANNOT_COMPILE: typ.Final[re.Pattern[str]] = re.compile(
    r"could not compile", re.IGNORECASE
)
#: A CBMC failure, which is not a proof result.
_SOLVER_FAILURE: typ.Final[re.Pattern[str]] = re.compile(
    r"\bCBMC (?:failed|timed out)\b|\bFailed to run cbmc\b|"
    r"CBMC appears to have run out of memory",
    re.IGNORECASE,
)


@dc.dataclass(frozen=True, slots=True)
class ProofReport:
    """The named harness verdicts and the summary Kani printed for one run.

    Attributes
    ----------
    outcomes : tuple of HarnessOutcome
        Verdicts in the order the harnesses were checked.
    verified_count : int
        Count taken from the manual summary.
    failed_count : int
        Count taken from the manual summary.
    total_count : int
        Count taken from the manual summary.
    """

    outcomes: tuple[HarnessOutcome, ...]
    verified_count: int
    failed_count: int
    total_count: int

    @property
    def verified_names(self) -> frozenset[str]:
        """The harnesses Kani verified."""
        return frozenset(item.name for item in self.outcomes if item.verified)

    @property
    def failed_names(self) -> frozenset[str]:
        """The harnesses Kani failed to verify."""
        return frozenset(item.name for item in self.outcomes if not item.verified)


def _bounded(transcript: str) -> str:
    """Return the transcript, refusing one larger than the documented ceiling."""
    if not isinstance(transcript, str):
        msg = "proof transcript must be text"
        raise ProofTranscriptError(msg)
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        msg = f"proof transcript exceeds the {MAX_TRANSCRIPT_BYTES}-byte ceiling"
        raise ProofTranscriptError(msg)
    return transcript


def _diagnose(transcript: str) -> None:
    """Refuse a transcript whose failure is compilation or the solver."""
    solver_failure = _SOLVER_FAILURE.search(transcript) is not None
    compile_failure = _has_compile_failure(transcript)
    if solver_failure:
        _raise_solver_failure(has_compile_failure=compile_failure)
    if compile_failure:
        msg = "proof transcript reports a compile error, not a verification failure"
        raise ProofCompileError(msg)


def _has_compile_failure(transcript: str) -> bool:
    """Say whether the transcript reports a compiler diagnostic."""
    return (
        _COMPILE_ERROR.search(transcript) is not None
        or _CANNOT_COMPILE.search(transcript) is not None
    )


def _raise_solver_failure(*, has_compile_failure: bool) -> typ.NoReturn:
    """Raise the solver failure, naming an accompanying compiler error."""
    if has_compile_failure:
        msg = "proof transcript names both a compiler error and a solver failure"
    else:
        msg = "proof transcript reports a CBMC solver failure, not a proof result"
    raise ProofSolverError(msg)


def parse_proof_report(transcript: str) -> ProofReport:
    """Return the named verdicts one Kani transcript states.

    Parameters
    ----------
    transcript : str
        Combined standard output and error of one `kani` run.

    Returns
    -------
    ProofReport
        The harness verdicts and the counts their manual summary states.

    Raises
    ------
    ProofCompileError
        Raised when the transcript reports a compilation failure.
    ProofSolverError
        Raised when the transcript reports a CBMC failure.
    ProofTranscriptError
        Raised for an oversized transcript, a run that found no harnesses, a
        missing or duplicated verdict, a missing or duplicated summary, or a
        summary whose counts or named failures contradict the verdicts.
    """
    text = _bounded(transcript)
    _diagnose(text)
    outcomes = _outcomes(text)
    summary = summary_counts(text)
    _require_summary_agrees(summary, outcomes)
    return ProofReport(
        outcomes=outcomes,
        verified_count=summary.verified,
        failed_count=summary.failed,
        total_count=summary.total,
    )


def _outcomes(text: str) -> tuple[HarnessOutcome, ...]:
    """Return the named verdicts, refusing a transcript with none."""
    sections = harness_sections(text)
    if not sections:
        _raise_missing_harnesses(text)
    return tuple(itertools.starmap(harness_outcome, sections.items()))


def _raise_missing_harnesses(text: str) -> typ.NoReturn:
    """Raise the failure a transcript with no harness section states."""
    if has_no_harness_message(text):
        msg = "proof transcript reports that no proof harnesses were found"
    else:
        msg = "proof transcript contains no named harness verdicts"
    raise ProofTranscriptError(msg)


def _require_summary_agrees(
    summary: Summary, outcomes: tuple[HarnessOutcome, ...]
) -> None:
    """Require the summary's counts and named failures to match the verdicts."""
    verified_names = frozenset(item.name for item in outcomes if item.verified)
    failed_names = frozenset(item.name for item in outcomes if not item.verified)
    if (summary.verified, summary.failed, summary.total) != (
        len(verified_names),
        len(failed_names),
        len(outcomes),
    ):
        msg = (
            "proof transcript manual summary "
            f"({summary.verified} verified, {summary.failed} failed, "
            f"{summary.total} total) contradicts the {len(verified_names)} "
            f"verified and {len(failed_names)} failed harness verdicts"
        )
        raise ProofTranscriptError(msg)
    if summary.named_failures != failed_names:
        msg = (
            "proof transcript manual summary names failures "
            f"{sorted(summary.named_failures)} but the verdicts name "
            f"{sorted(failed_names)}"
        )
        raise ProofTranscriptError(msg)


def require_named_harnesses(
    transcript: str,
    *,
    verified: cabc.Iterable[str],
    failed: cabc.Iterable[str],
    qualified: bool = False,
) -> ProofReport:
    """Require the exact expected harness sets and their expected statuses.

    Parameters
    ----------
    transcript : str
        Combined standard output and error of one `kani` run.
    verified : collections.abc.Iterable of str
        Harnesses that must have verified.
    failed : collections.abc.Iterable of str
        Harnesses that must have failed verification.
    qualified : bool
        Compare on each harness's final `::` component. Kani prints a harness
        inside a module with its module path, so a fixture's `test_sum` appears
        as `simple_lib::kani_tests::test_sum`. The set must still match exactly;
        only the printed prefix is ignored.

    Returns
    -------
    ProofReport
        The validated report.

    Raises
    ------
    ProofTranscriptError
        Raised when the transcript is malformed, or when its harness sets or
        statuses differ from the expectation.
    """
    report = parse_proof_report(transcript)
    expected_verified = frozenset(verified)
    expected_failed = frozenset(failed)
    _reject_collisions(report, qualified=qualified)
    comparison = _Comparison(qualified=qualified)
    comparison.require(
        expected_verified,
        report.verified_names,
        report.failed_names,
        status="verify",
    )
    comparison.require(
        expected_failed,
        report.failed_names,
        report.verified_names,
        status="fail",
    )
    return report


@dc.dataclass(frozen=True, slots=True)
class _Comparison:
    """How reported harness names are compared with expectations.

    Attributes
    ----------
    qualified : bool
        Whether an expectation may name only part of a module-qualified print.
    """

    qualified: bool

    def matches(self, expected: str, reported: str) -> bool:
        """Say whether one reported name satisfies one expectation.

        Default mode compares the exact reported name, so a harness in another
        module cannot stand in for the expected one. Qualified mode accepts the
        expectation as a full suffix of the print, so a crate-relative name such
        as `kani_tests::test_sum` matches what Kani prints.
        """
        if not self.qualified:
            return reported == expected
        return reported == expected or reported.endswith(f"::{expected}")

    def require(
        self,
        wanted: frozenset[str],
        reported: frozenset[str],
        opposite: frozenset[str],
        *,
        status: str,
    ) -> None:
        """Require each expectation to name one reported harness and no more."""
        unused = set(reported)
        for name in sorted(wanted):
            matched = {item for item in unused if self.matches(name, item)}
            if not matched:
                _require_status(frozenset({name}), opposite, expected=status)
            if len(matched) > 1:
                msg = (
                    f"harness {name!r} matches more than one reported harness: "
                    f"{sorted(matched)}"
                )
                raise ProofTranscriptError(msg)
            unused -= matched
        if unused:
            _require_unexpected(frozenset(unused), status)


def _matching(names: frozenset[str], *, qualified: bool) -> frozenset[str]:
    """Return the names to compare, unchanged.

    The comparison itself distinguishes the two modes; both keep the reported
    name so a qualified print is never reduced before it is compared.
    """
    del qualified
    return names


def _reject_collisions(report: ProofReport, *, qualified: bool) -> None:
    """Refuse two printed harnesses that a bare expectation cannot tell apart.

    A bare expectation names only a final component. If two reported harnesses
    share one, that expectation would be satisfied by either, so the transcript
    is reported as ambiguous rather than silently merged.
    """
    if not qualified:
        return
    bare = [item.name.rsplit("::", 1)[-1] for item in report.outcomes]
    repeated = sorted({name for name in bare if bare.count(name) > 1})
    if repeated:
        msg = (
            "proof transcript prints distinct qualified harnesses with the same "
            f"name, so the set is ambiguous: {repeated}"
        )
        raise ProofTranscriptError(msg)


def _require_status(
    missing: frozenset[str], other: frozenset[str], *, expected: str
) -> None:
    """Refuse a required harness that is absent or carries the other status."""
    if not missing:
        return
    name = min(missing)
    if name in other:
        msg = f"harness {name!r} {_OPPOSITE_STATUS[expected]}"
        raise ProofTranscriptError(msg)
    msg = f"transcript did not {expected} harness {name!r}"
    raise ProofTranscriptError(msg)


def _require_unexpected(extra: frozenset[str], status: str) -> None:
    """Refuse a harness whose status the expectation did not name."""
    if not extra:
        return
    name = min(extra)
    msg = f"transcript {status} unexpected harness {name!r}"
    raise ProofTranscriptError(msg)


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
