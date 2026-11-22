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

from rust_prover_tools.errors import ProverToolError

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

#: `cargo kani --jobs N` prefixes some lines with the rayon thread index. The
#: verdict and summary lines are part of a multi-line block, so only their first
#: line carries the marker; every pattern therefore tolerates it optionally.
_PREFIX = r"(?:Thread [0-9]+: )?"
_HARNESS_START: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PREFIX}Checking harness (?P<name>\S+?)\.\.\.$", re.MULTILINE
)
_VERDICT: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PREFIX}VERIFICATION:- (?P<verdict>SUCCESSFUL|FAILED)(?P<note>.*)$",
    re.MULTILINE,
)
_SUMMARY: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PREFIX}Complete - (?P<verified>[0-9]+) successfully verified harnesses, "
    rf"(?P<failed>[0-9]+) failures, (?P<total>[0-9]+) total\.$",
    re.MULTILINE,
)
_NO_HARNESSES: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PREFIX}No proof harnesses .* were found to verify\.$", re.MULTILINE
)
_FAILED_FOR: typ.Final[re.Pattern[str]] = re.compile(
    rf"^{_PREFIX}Verification failed for - (?P<name>\S+)$", re.MULTILINE
)
_COMPILE_ERROR: typ.Final[re.Pattern[str]] = re.compile(
    r"^error(?:\[[A-Za-z0-9]+\])?: ", re.MULTILINE
)
_CANNOT_COMPILE: typ.Final[re.Pattern[str]] = re.compile(
    r"could not compile", re.IGNORECASE
)
_SOLVER_FAILURE: typ.Final[re.Pattern[str]] = re.compile(
    r"\bCBMC (?:failed|timed out)\b|\bFailed to run cbmc\b|"
    r"CBMC appears to have run out of memory",
    re.IGNORECASE,
)


#: How to describe a harness that carried the status opposite the expectation.
_OPPOSITE_STATUS: typ.Final[dict[str, str]] = {
    "verify": "failed but was expected to verify",
    "fail": "verified but was expected to fail",
}


class ProofTranscriptError(ProverToolError):
    """A Kani transcript does not state one consistent proof result."""


class ProofCompileError(ProofTranscriptError):
    """Kani failed while compiling the fixture, not while proving it."""


class ProofSolverError(ProofTranscriptError):
    """The solver failed, so no harness obtained a verification verdict."""


@dc.dataclass(frozen=True, slots=True)
class HarnessOutcome:
    """One harness and the verdict Kani reported for it.

    Attributes
    ----------
    name : str
        Harness name as printed after `Checking harness`.
    verified : bool
        `True` for a successful verification, `False` for a failed one.
    """

    name: str
    verified: bool


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
    compile_failure = (
        _COMPILE_ERROR.search(transcript) is not None
        or _CANNOT_COMPILE.search(transcript) is not None
    )
    solver_failure = _SOLVER_FAILURE.search(transcript) is not None
    if solver_failure and compile_failure:
        msg = "proof transcript names both a compiler error and a solver failure"
        raise ProofSolverError(msg)
    if solver_failure:
        msg = "proof transcript reports a CBMC solver failure, not a proof result"
        raise ProofSolverError(msg)
    if compile_failure:
        msg = "proof transcript reports a compile error, not a verification failure"
        raise ProofCompileError(msg)


def _sections(transcript: str) -> dict[str, str]:
    """Split a transcript into the text belonging to each named harness.

    A duplicate harness start is refused because two sections for one name
    would make "the" verdict for that harness depend on document order, which
    is exactly the ambiguity a contradictory transcript is meant to expose.
    """
    starts = list(_HARNESS_START.finditer(transcript))
    sections: dict[str, str] = {}
    for index, start in enumerate(starts):
        name = start.group("name")
        if name in sections:
            msg = f"proof transcript checks harness {name!r} more than once"
            raise ProofTranscriptError(msg)
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
        sections[name] = transcript[start.end() : end]
    return sections


def _outcome(name: str, body: str) -> HarnessOutcome:
    """Return the single verdict a harness section states."""
    verdicts = _VERDICT.findall(body)
    if not verdicts:
        msg = f"proof transcript states no verdict for harness {name!r}"
        raise ProofTranscriptError(msg)
    if len(verdicts) > 1:
        msg = f"proof transcript states more than one verdict for harness {name!r}"
        raise ProofTranscriptError(msg)
    verdict = verdicts[0][0]
    return HarnessOutcome(name=name, verified=verdict == "SUCCESSFUL")


def _summary_counts(transcript: str) -> tuple[int, int, int, frozenset[str]]:
    """Return the single manual summary and the harnesses it names as failed."""
    summaries = _SUMMARY.findall(transcript)
    if not summaries:
        msg = "proof transcript does not report a manual harness summary"
        raise ProofTranscriptError(msg)
    if len(summaries) > 1:
        msg = "proof transcript reports more than one manual harness summary"
        raise ProofTranscriptError(msg)
    verified, failed, total = (int(value) for value in summaries[0])
    named_failures = frozenset(
        match.group("name") for match in _FAILED_FOR.finditer(transcript)
    )
    return verified, failed, total, named_failures


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
    sections = _sections(text)
    if not sections:
        if _NO_HARNESSES.search(text) is not None:
            msg = "proof transcript reports that no proof harnesses were found"
            raise ProofTranscriptError(msg)
        msg = "proof transcript contains no named harness verdicts"
        raise ProofTranscriptError(msg)
    outcomes = tuple(itertools.starmap(_outcome, sections.items()))
    verified, failed, total, named_failures = _summary_counts(text)
    verified_names = frozenset(item.name for item in outcomes if item.verified)
    failed_names = frozenset(item.name for item in outcomes if not item.verified)
    if (verified, failed, total) != (
        len(verified_names),
        len(failed_names),
        len(outcomes),
    ):
        msg = (
            "proof transcript manual summary "
            f"({verified} verified, {failed} failed, {total} total) contradicts "
            f"the {len(verified_names)} verified and {len(failed_names)} failed "
            "harness verdicts"
        )
        raise ProofTranscriptError(msg)
    if named_failures != failed_names:
        msg = (
            "proof transcript manual summary names failures "
            f"{sorted(named_failures)} but the verdicts name {sorted(failed_names)}"
        )
        raise ProofTranscriptError(msg)
    return ProofReport(
        outcomes=outcomes,
        verified_count=verified,
        failed_count=failed,
        total_count=total,
    )


def require_named_harnesses(
    transcript: str,
    *,
    verified: cabc.Iterable[str],
    failed: cabc.Iterable[str],
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
    _require_status(
        expected_verified - report.verified_names,
        report.failed_names,
        expected="verify",
    )
    _require_unexpected(report.verified_names - expected_verified, "verified")
    _require_status(
        expected_failed - report.failed_names,
        report.verified_names,
        expected="fail",
    )
    _require_unexpected(report.failed_names - expected_failed, "failed")
    return report


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
