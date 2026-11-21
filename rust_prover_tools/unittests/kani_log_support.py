"""Share the committed Kani corpus and its fact comparison between tests.

The corpus is the normative grammar: every accepted log must parse to exactly
the facts committed beside it, and every refused log must be refused for the
recorded reason. Keeping the loader and comparison here means the reader tests
and any later classifier tests compare against one definition of "the recorded
facts" rather than two.

Examples
--------
>>> first = sorted(LOGS)[0]
>>> isinstance(first, str)
True
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log import HarnessBlock, KaniLog

__all__ = (
    "COMPLETE",
    "CORPUS",
    "EXPECTED",
    "HARNESS_START",
    "LOGS",
    "MANUAL",
    "REPORT_PREFIXES",
    "RESULTS",
    "SUMMARY",
    "TIME",
    "VERIFICATION",
    "LogAcceptedError",
    "assert_matches_expected",
    "lines_of",
    "log_text",
    "refused",
)

#: Repository fixture directory holding the fifteen captured logs.
CORPUS: typ.Final[Path] = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs"
)

EXPECTED: typ.Final[dict[str, typ.Any]] = json.loads(
    (CORPUS / "expected.json").read_text(encoding="utf-8")
)

#: One committed expectation per captured log.
LOGS: typ.Final[dict[str, typ.Any]] = EXPECTED["logs"]

HARNESS_START = "Checking harness "
RESULTS = "RESULTS:"
SUMMARY = "SUMMARY:"
TIME = "Verification Time: "
VERIFICATION = "VERIFICATION:- "
MANUAL = "Manual Harness Summary:"
COMPLETE = "Complete - "

#: Lines that carry report structure. Generated chatter never includes one,
#: because a report line at a random position is a different claim.
REPORT_PREFIXES: typ.Final[tuple[str, ...]] = (
    HARNESS_START,
    RESULTS,
    SUMMARY,
    "Check ",
    "\t - ",
    " ** ",
    VERIFICATION,
    TIME,
    "Failed Checks: ",
    " File: ",
    MANUAL,
    "Verification failed for - ",
    COMPLETE,
    "Wrote list results to ",
    "warning: Found the following unsupported constructs:",
)


def log_text(name: str) -> str:
    """Return one corpus log as text.

    Parameters
    ----------
    name : str
        Corpus entry name without its extension.

    Returns
    -------
    str
        The captured log.
    """
    return (CORPUS / f"{name}.log").read_text(encoding="utf-8")


def lines_of(name: str) -> list[str]:
    """Return one corpus log split into lines.

    Parameters
    ----------
    name : str
        Corpus entry name without its extension.

    Returns
    -------
    list[str]
        The captured log's lines, without terminators.
    """
    return log_text(name).splitlines()


def assert_matches_expected(name: str, log: KaniLog) -> None:
    """Compare a parsed log with the committed facts for one corpus entry.

    Parameters
    ----------
    name : str
        Corpus entry name without its extension.
    log : KaniLog
        Parsed facts to compare.
    """
    entry = LOGS[name]
    assert log.banner_version == entry["banner"], "banner mismatch"
    assert log.format == entry["format"], "format mismatch"
    assert list(log.unsupported_constructs) == entry["unsupported"], (
        "unsupported constructs mismatch"
    )
    _assert_manual(entry, log)
    expected_blocks = entry.get("blocks", [])
    assert [block.name for block in log.blocks] == [
        expected["name"] for expected in expected_blocks
    ], "block sequence mismatch"
    blocks = {block.name: block for block in log.blocks}
    for expected in expected_blocks:
        got = blocks.get(expected["name"])
        assert got is not None, f"missing block {expected['name']}"
        _assert_block(expected, got)


def _assert_manual(entry: dict[str, typ.Any], log: KaniLog) -> None:
    """Compare the manual summary with its committed facts."""
    expected = entry["manual"]
    if expected is None:
        assert log.manual is None, "manual summary should be absent"
        return
    assert log.manual is not None, "manual summary should be present"
    assert (
        log.manual.verified,
        log.manual.failures,
        log.manual.total,
        list(log.manual.failed),
    ) == (
        expected["verified"],
        expected["failures"],
        expected["total"],
        expected["failed"],
    ), "manual summary mismatch"


def _assert_block(expected: dict[str, typ.Any], got: HarnessBlock) -> None:
    """Compare one harness block with its committed facts."""
    name = expected["name"]
    assert got.verdict == expected["verdict"], f"{name}: verdict"
    assert got.failed_count == expected["failed"], f"{name}: failed count"
    assert got.total_count == expected["total"], f"{name}: total count"
    assert [got.other_count, got.other_kind] == expected["other"], (
        f"{name}: other count"
    )
    assert got.timed_out == expected["timed_out"], f"{name}: timeout flag"
    assert got.unwinding_reported == expected["unwinding_reported"], (
        f"{name}: unwinding flag"
    )
    assert got.panics_as_expected == expected["panics_as_expected"], (
        f"{name}: expected-panic flag"
    )
    assert [(item.kind, item.status) for item in got.properties] == [
        tuple(item) for item in expected["checks"]
    ], f"{name}: checks"
    assert [(item.description, item.status) for item in got.covers] == [
        tuple(item) for item in expected["covers"]
    ], f"{name}: covers"


class LogAcceptedError(AssertionError):
    """The log parsed when the test required a refusal."""


def refusal_of(lines: list[str]) -> str:
    """Return the reason a log is refused, failing when it is accepted.

    Parameters
    ----------
    lines : list[str]
        Log lines to parse.

    Returns
    -------
    str
        The recorded structural reason.
    """
    from rust_prover_tools.kani_log import KaniLogError, parse

    try:
        parse("\n".join(lines))
    except KaniLogError as error:
        return error.reason
    raise LogAcceptedError


# ``refused`` is the name the mutation tests read best; keep both spellings
# pointing at one implementation rather than duplicating it.
refused = refusal_of
