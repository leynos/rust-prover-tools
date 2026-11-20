"""Build the per-harness facts the Kani decision suite enumerates.

Exhaustive enumeration is only exhaustive if the generator is.  Rather than
sample a few handwritten logs, this module names every decision-relevant class
of per-harness fact - two facts belong to the same class when no reader of the
analysis can tell them apart - and the suite takes the full cartesian product
of those classes for one, two and three harnesses.

Nothing here may be more generous than a real log.  A constructed block carries
the same fields the extractor fills, including summary counts, failure
explanations and the echoed solver command, so a fact set the parser could never
produce is still refused rather than quietly accepted.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import (
    CONTROL,
    SUPPORTED_KANI_VERSIONS,
    FailedCheck,
    Harness,
    HarnessBlock,
    KaniAnalysis,
    KaniLog,
    Manifest,
    ManualSummary,
    PropertyResult,
    analyse_kani_log,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "ABSENT",
    "AS_INTENDED",
    "FAILURE",
    "FOREIGN_FAILURE",
    "FOREIGN_UNWIND_FAILURE",
    "NOTHING",
    "OTHERWISE",
    "SUCCESS",
    "TIMEOUT",
    "UNATTRIBUTED_FAILURE",
    "UNDETERMINED",
    "UNSUPPORTED_ASSERTION",
    "UNSUPPORTED_CHECK",
    "UNWIND_FAILURE",
    "HarnessFact",
    "block_of",
    "build",
    "decide_row",
    "decision_for",
    "manifest_of",
)

NOTHING = "NONE"

SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
TIMEOUT = "TIMEOUT"
UNDETERMINED = "UNDETERMINED"
UNWIND_FAILURE = "UNWIND_FAILURE"
UNSUPPORTED_CHECK = "UNSUPPORTED_CHECK"
UNSUPPORTED_ASSERTION = "UNSUPPORTED_ASSERTION"
FOREIGN_FAILURE = "FOREIGN_FAILURE"
FOREIGN_UNWIND_FAILURE = "FOREIGN_UNWIND_FAILURE"
UNATTRIBUTED_FAILURE = "UNATTRIBUTED_FAILURE"
ABSENT = "ABSENT"

SATISFIED = "SATISFIED"
UNREACHABLE = "UNREACHABLE"
UNSATISFIABLE = "UNSATISFIABLE"
COVER_STATUSES = (NOTHING, SATISFIED, UNREACHABLE, UNSATISFIABLE)
AS_INTENDED = "as-intended"
OTHERWISE = "otherwise"

UNSUPPORTED_WORDING = "TerminatorKind::InlineAsm is not currently supported by Kani."
LIBRARY_CHECK = "core::fmt::Arguments::<'_>::from_str.assertion.1"
LIBRARY_OWNER = LIBRARY_CHECK.rpartition(".")[0].rpartition(".")[0]
BOUNDED_SOLVER = (
    "[Kani] Running: `cbmc --object-bits 16 --unwind 2 --sat-solver cadical`"
)
UNBOUNDED_SOLVER = "[Kani] Running: `cbmc --object-bits 16 --sat-solver cadical`"
FAILING = (
    FAILURE,
    AS_INTENDED,
    OTHERWISE,
    UNWIND_FAILURE,
    UNSUPPORTED_CHECK,
    UNSUPPORTED_ASSERTION,
    FOREIGN_FAILURE,
    FOREIGN_UNWIND_FAILURE,
)


@dc.dataclass(frozen=True, slots=True)
class HarnessFact:
    """One harness reduced to the facts a decision reads.

    ``role`` is ``None`` for a harness the log reports but the manifest does not
    declare, which the analysis calls a structural error.  ``should_panic`` and
    ``note`` are separate because Kani's verdict note, not the manifest, is what
    excuses a failed assertion.
    """

    name: str
    role: str | None
    status: str
    cover: str = NOTHING
    expected: str = AS_INTENDED
    should_panic: bool = False
    note: bool = False
    bounded: bool = False
    cover_description: str | None = None

    @property
    def witness(self) -> str:
        """Name the cover this harness declares, if any."""
        return self.cover_description or f"the witness of {self.name}"

    @property
    def verdict(self) -> str:
        """The verdict Kani would print for this fact set."""
        if self.status in {SUCCESS, UNDETERMINED} or self.panic_excused:
            return "SUCCESSFUL"
        return "FAILED"

    @property
    def panic_excused(self) -> bool:
        """Whether Kani would excuse this failure as the panic it expected."""
        if self.status != FAILURE:
            return False
        return self.should_panic and self.note


def _foreign_checks(name: str) -> list[PropertyResult]:
    """Build a block whose own check passes while a library check fails."""
    return [
        PropertyResult(
            LIBRARY_CHECK,
            "FAILURE",
            "attempt to shift left with overflow",
        ),
        PropertyResult(
            f"{name}.assertion.1",
            "SUCCESS",
            "This is a placeholder message; Kani doesn't support message "
            "formatted at runtime",
        ),
    ]


def _foreign_unwind_checks(name: str) -> list[PropertyResult]:
    """Build a block whose own check passes while a library loop's bound fails."""
    return [
        PropertyResult(
            f"{LIBRARY_OWNER}.unwind.0",
            "FAILURE",
            "unwinding assertion loop 0",
        ),
        PropertyResult(f"{name}.assertion.1", "SUCCESS", "the assertion holds"),
    ]


def _owned_checks(fact: HarnessFact) -> list[PropertyResult]:
    """Build the single check a harness owns, when its class names one."""
    descriptions = {
        SUCCESS: ("assertion.1", "SUCCESS", "the assertion holds"),
        FAILURE: ("assertion.1", "FAILURE", "the assertion fails"),
        AS_INTENDED: ("assertion.1", "FAILURE", "the seeded fault fires"),
        OTHERWISE: ("assertion.2", "FAILURE", "an unrelated check fails"),
        UNDETERMINED: (
            "assertion.1",
            "UNDETERMINED",
            "Kani could not decide this check",
        ),
        UNWIND_FAILURE: ("unwind.0", "FAILURE", "unwinding assertion loop 0"),
        UNSUPPORTED_CHECK: (
            "unsupported_construct.1",
            "FAILURE",
            UNSUPPORTED_WORDING,
        ),
        UNSUPPORTED_ASSERTION: ("assertion.1", "FAILURE", UNSUPPORTED_WORDING),
        UNATTRIBUTED_FAILURE: (
            "assertion.1",
            "SUCCESS",
            "the assertion holds",
        ),
    }
    check = descriptions.get(fact.status)
    if check is None:
        return []
    return [PropertyResult(f"{fact.name}.{check[0]}", check[1], check[2])]


def _checks(fact: HarnessFact) -> list[PropertyResult]:
    """Build the checks this fact's block reports, in the order Kani lists them."""
    if fact.status == FOREIGN_FAILURE:
        checks = _foreign_checks(fact.name)
    elif fact.status == FOREIGN_UNWIND_FAILURE:
        checks = _foreign_unwind_checks(fact.name)
    else:
        checks = _owned_checks(fact)
    if fact.cover != NOTHING:
        checks.append(
            PropertyResult(
                f"{fact.name}.cover.1",
                fact.cover,
                fact.witness,
            ),
        )
    return checks


def block_of(fact: HarnessFact) -> HarnessBlock | None:
    """Build the harness block a log would report, or ``None`` when absent."""
    if fact.status == ABSENT:
        return None
    checks = tuple(_checks(fact))
    covers = tuple(item for item in checks if item.is_cover)
    failed = sum(1 for item in checks if item.status == "FAILURE")
    failures = (
        (FailedCheck(checks[0].description, "src/harnesses.rs", 7),)
        if fact.status in FAILING
        else ()
    )
    is_timeout = fact.status == TIMEOUT
    return HarnessBlock(
        name=fact.name,
        properties=checks,
        failures=failures,
        failed_count=None if is_timeout else failed,
        total_count=None if is_timeout else len(checks),
        other_count=sum(1 for item in checks if item.status == UNDETERMINED),
        other_kind="undetermined" if fact.status == UNDETERMINED else None,
        covered_satisfied=sum(1 for item in covers if item.status == SATISFIED) or None,
        covered_total=len(covers) or None,
        covered_unreachable=sum(1 for item in covers if item.status == UNREACHABLE)
        or None,
        verdict=fact.verdict,
        panics_as_expected=fact.should_panic and fact.note,
        solver_command=BOUNDED_SOLVER if fact.bounded else UNBOUNDED_SOLVER,
        seen_verification_time=not is_timeout,
        timed_out=is_timeout,
        unwinding_reported=fact.status == UNWIND_FAILURE,
        has_results=not is_timeout,
    )


def manifest_of(facts: cabc.Iterable[HarnessFact]) -> Manifest:
    """Build the manifest that declares exactly the harnesses with a role."""
    entries = []
    for fact in facts:
        if fact.role is None:
            continue
        covers = (fact.witness,) if fact.cover != NOTHING else ()
        expected = "assertion.1" if fact.expected == AS_INTENDED else "assertion.2"
        entries.append(
            Harness(
                fact.name,
                fact.role,
                covers,
                expected if fact.role == CONTROL else None,
                fact.should_panic,
            ),
        )
    return Manifest(tuple(entries))


def build(
    facts: cabc.Sequence[HarnessFact],
    *,
    log_format: str = "regular",
    unsupported_constructs: tuple[str, ...] = (),
) -> tuple[KaniLog, Manifest]:
    """Build one log and its manifest from a sequence of harness facts."""
    manifest = manifest_of(facts)
    version = next(iter(SUPPORTED_KANI_VERSIONS))
    if log_format != "regular":
        return KaniLog(version, log_format, (), None, unsupported_constructs), manifest
    blocks = tuple(
        block for block in (block_of(fact) for fact in facts) if block is not None
    )
    summary = ManualSummary(
        sum(1 for block in blocks if block.verdict == "SUCCESSFUL"),
        sum(1 for block in blocks if block.verdict == "FAILED"),
        len(blocks),
        tuple(block.name for block in blocks if block.verdict == "FAILED"),
    )
    return KaniLog(
        version, log_format, blocks, summary, unsupported_constructs
    ), manifest


def decision_for(
    facts: cabc.Sequence[HarnessFact],
    *,
    log_format: str = "regular",
    unsupported_constructs: tuple[str, ...] = (),
) -> KaniAnalysis:
    """Decide one constructed fact set with the same options :func:`build` takes."""
    return analyse_kani_log(
        *build(
            facts,
            log_format=log_format,
            unsupported_constructs=unsupported_constructs,
        ),
    )


TABLE_STATUSES = {"UNSUPPORTED_FAILURE": UNSUPPORTED_CHECK}


def facts_of_row(row: dict[str, typ.Any]) -> tuple[HarnessFact, ...]:
    """Translate one decision-table row into the facts that describe it."""
    facts = []
    for entry in row["harnesses"]:
        covers = entry.get("covers") or ()
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
