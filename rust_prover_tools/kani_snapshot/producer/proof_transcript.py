"""Primitive readings of one Kani proof transcript.

The pinned Kani driver states a run in three places: a named `Checking harness`
start, that harness's own `VERIFICATION:-` verdict, and a final manual summary.
This module turns each of those into a frozen value. The policy that decides
whether a combination of values is an acceptable result lives in
`proof_report`, so the primitives here stay small and reusable.

`cargo kani --jobs N` prefixes some lines with a rayon thread index. The
verdict and summary bodies are multi-line blocks, so only their first line
carries the marker; every pattern therefore tolerates it optionally.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "HarnessOutcome",
    "ProofCompileError",
    "ProofSolverError",
    "ProofTranscriptError",
    "Summary",
    "harness_outcome",
    "harness_sections",
    "has_no_harness_message",
    "summary_counts",
)

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
class Summary:
    """The manual summary a transcript states.

    Attributes
    ----------
    verified : int
        Verified harnesses the summary counts.
    failed : int
        Failed harnesses the summary counts.
    total : int
        Total harnesses the summary counts.
    named_failures : frozenset of str
        Harnesses the summary's `Verification failed for` lines name.
    """

    verified: int
    failed: int
    total: int
    named_failures: frozenset[str]


def harness_sections(transcript: str) -> dict[str, str]:
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


def harness_outcome(name: str, body: str) -> HarnessOutcome:
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


def summary_counts(transcript: str) -> Summary:
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
    return Summary(
        verified=verified,
        failed=failed,
        total=total,
        named_failures=named_failures,
    )


def has_no_harness_message(transcript: str) -> bool:
    """Say whether the transcript reports that no proof harnesses were found."""
    return _NO_HARNESSES.search(transcript) is not None
