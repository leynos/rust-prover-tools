"""Extract per-harness facts from a Kani log, or refuse the log.

Unknown chatter *between* reports is accepted, so a Kani release that adds a
post-processing message does not break the parser.  Refusal is for structure
the parser cannot account for inside a report: a missing banner or ``Manual
Harness Summary``, a truncated or duplicated harness block, a block that ends
without its verdict, summary counts that disagree with the checks listed, or an
unrecognised line inside a report block.
"""

from __future__ import annotations

import dataclasses as dc

from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog, ManualSummary
from rust_prover_tools.kani_log.reader import Reader
from rust_prover_tools.kani_log.syntax import (
    HARNESS_START,
    LIST_WRITTEN,
    NO_UNWINDING_ASSERTIONS_FLAG,
    SUPPORTED_KANI_VERSIONS,
    UNSUPPORTED_PRELUDE,
    UNWINDING_BOUND_FLAGS,
    KaniLogError,
)

__all__ = ("parse_kani_log", "unwinding_checks_enabled")


def parse_kani_log(text: str) -> KaniLog:
    """Read one Kani log into per-harness facts, or refuse it.

    Parameters
    ----------
    text : str
        The complete captured output of one Kani invocation, with standard
        output followed by standard error.

    Returns
    -------
    KaniLog
        The banner, output format, every harness block, the manual summary and
        the constructs Kani named as unsupported.

    Raises
    ------
    KaniLogError
        If the log is truncated, duplicated, interleaved, count-inconsistent,
        from an unsupported version, or otherwise not a complete Kani report.

    Examples
    --------
    A log without a banner cannot be attributed to a Kani release, so it is
    refused with ``structural error: the log does not begin with a Kani
    banner``.

    """
    reader = Reader(text.splitlines())
    version = _require_supported_banner(reader)
    return _reconcile(version, _scan(reader))


def _require_supported_banner(reader: Reader) -> str:
    """Return the banner's release, refusing a log from another release."""
    banner = reader.banner()
    if banner is None:
        msg = "the log does not begin with a Kani banner"
        raise KaniLogError(msg)
    version = banner[0]
    if version not in SUPPORTED_KANI_VERSIONS:
        msg = (
            f"the log is from Kani {version}, but this parser reads "
            f"{list(SUPPORTED_KANI_VERSIONS)}"
        )
        raise KaniLogError(msg)
    return version


@dc.dataclass(frozen=True, slots=True)
class _ObservedLog:
    """The parts a scan collected before they are reconciled."""

    blocks: tuple[HarnessBlock, ...]
    manual: ManualSummary | None
    is_list: bool
    unsupported: tuple[str, ...]


def _scan(reader: Reader) -> _ObservedLog:
    """Read report markers until the log is exhausted."""
    blocks: list[HarnessBlock] = []
    manual: ManualSummary | None = None
    unsupported: list[str] = []
    is_list = False
    while not reader.exhausted:
        line = reader.peek()
        if line == "Manual Harness Summary:":
            if manual is not None:
                msg = f"{reader.where()}: a second manual harness summary"
                raise KaniLogError(msg)
            manual = reader.manual()
        elif line == UNSUPPORTED_PRELUDE:
            unsupported.extend(reader.unsupported_constructs())
        elif line.startswith(LIST_WRITTEN):
            is_list = True
            reader.take()
        elif header := HARNESS_START.match(line):
            blocks.append(reader.block(header))
        else:
            reader.take()
    return _ObservedLog(
        tuple(blocks),
        manual,
        is_list,
        tuple(unsupported),
    )


def _reconcile(version: str, seen: _ObservedLog) -> KaniLog:
    """Reconcile the log's parts, refusing any combination that cannot be real."""
    if seen.is_list:
        if seen.blocks:
            msg = "the log claims to be both an inventory and a verification run"
            raise KaniLogError(msg)
        return KaniLog(version, "inventory", (), None, seen.unsupported)
    if not seen.blocks:
        msg = "the log reports no harness results"
        raise KaniLogError(msg)
    if seen.manual is None:
        msg = "the log is truncated: it has no manual harness summary"
        raise KaniLogError(msg)
    _require_unique_harnesses(seen.blocks)
    _require_summary_counts(seen.blocks, seen.manual)
    return KaniLog(version, "regular", seen.blocks, seen.manual, seen.unsupported)


def _require_unique_harnesses(blocks: tuple[HarnessBlock, ...]) -> None:
    """Refuse a run that reports the same harness twice."""
    names = tuple(block.name for block in blocks)
    if len(set(names)) != len(names):
        msg = "a harness was reported twice in the same run"
        raise KaniLogError(msg)


def _require_summary_counts(
    blocks: tuple[HarnessBlock, ...],
    manual: ManualSummary,
) -> None:
    """Refuse a manual summary that disagrees with the blocks."""
    succeeded = tuple(block.name for block in blocks if block.succeeded)
    failed = tuple(block.name for block in blocks if block.verdict == "FAILED")
    if (len(succeeded), len(failed), len(blocks)) != (
        manual.verified,
        manual.failures,
        manual.total,
    ):
        msg = (
            f"the manual summary claims {manual.verified} verified and "
            f"{manual.failures} failed of {manual.total}, but the blocks "
            f"report {len(succeeded)} verified and {len(failed)} failed of "
            f"{len(blocks)}"
        )
        raise KaniLogError(msg)
    if manual.failed != failed:
        msg = (
            f"the manual summary names failures {list(manual.failed)}, but the "
            f"blocks report {list(failed)}"
        )
        raise KaniLogError(msg)


def unwinding_checks_enabled(block: HarnessBlock) -> bool | None:
    """Report whether the solver was asked to generate unwinding assertions.

    Parameters
    ----------
    block : HarnessBlock
        One parsed harness block.

    Returns
    -------
    bool | None
        ``True`` when the echoed CBMC command carries an unwinding bound and
        does not disable the assertions, ``False`` when it carries no bound or
        disables them, and ``None`` when no solver command was echoed.

    Notes
    -----
    The rule is CBMC's, not a guess: a bound is what makes CBMC emit the
    assertion in the first place, which is why Kani's driver never passes
    ``--unwinding-assertions`` and instead disables them with
    ``--no-unwinding-assertions``.

    """
    if block.solver_command is None:
        return None
    if NO_UNWINDING_ASSERTIONS_FLAG in block.solver_command:
        return False
    return any(flag in block.solver_command for flag in UNWINDING_BOUND_FLAGS)
