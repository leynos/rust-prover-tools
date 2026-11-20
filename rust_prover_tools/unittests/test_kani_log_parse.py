"""Prove the Kani parser accepts real logs and refuses malformed ones."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_log import (
    SUPPORTED_KANI_VERSIONS,
    HarnessBlock,
    KaniLog,
    KaniLogError,
    parse_kani_log,
    unwinding_checks_enabled,
)

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs"
)
#: Each qualified release keeps its own corpus directory, named by version.
CORPORA: typ.Final[tuple[Path, ...]] = tuple(
    FIXTURES / version for version in SUPPORTED_KANI_VERSIONS
)
CURRENT_CORPUS: typ.Final[Path] = CORPORA[0]


def corpus_expected(corpus: Path) -> dict[str, typ.Any]:
    """Return one corpus directory's recorded facts and decisions."""
    return typ.cast(
        "dict[str, typ.Any]",
        json.loads((corpus / "expected.json").read_text(encoding="utf-8")),
    )


EXPECTED: typ.Final[dict[str, dict[str, typ.Any]]] = {
    corpus.name: corpus_expected(corpus) for corpus in CORPORA
}
CORPUS_CASES: typ.Final[tuple[tuple[str, str], ...]] = tuple(
    (version, name)
    for version, entries in EXPECTED.items()
    for name in sorted(entries["logs"])
)
LOG_NAMES: typ.Final[tuple[str, ...]] = tuple(
    sorted(EXPECTED[CURRENT_CORPUS.name]["logs"])
)
CHATTER_LINE = "chatter inserted between reports"


def log_text(name: str, version: str | None = None) -> str:
    """Return one committed log exactly as the parser receives it.

    ``version`` selects a corpus directory; it defaults to the current
    qualified release so the single-release tests stay readable.
    """
    corpus = FIXTURES / (version or CURRENT_CORPUS.name)
    return (corpus / f"{name}.log").read_text(encoding="utf-8")


def block_facts(block: HarnessBlock) -> dict[str, object]:
    """Reduce one block to the facts the corpus records."""
    return {
        "name": block.name,
        "verdict": block.verdict,
        "failed": block.failed_count,
        "total": block.total_count,
        "other": [list(pair) for pair in block.other_counts],
        "timed_out": block.timed_out,
        "unwinding_reported": block.unwinding_reported,
        "panics_as_expected": block.panics_as_expected,
        "checks": [[item.kind, item.status] for item in block.properties],
        "covers": [[item.description, item.status] for item in block.covers],
        "failures": [[item.message, item.path, item.line] for item in block.failures],
    }


def refused(text: str) -> str:
    """Return the structural reason a log is refused.

    Parameters
    ----------
    text : str
        Log text expected to be refused.

    Returns
    -------
    str
        The parser's reason without its ``structural error:`` prefix.

    Raises
    ------
    AssertionError
        If the parser accepts the text.

    """
    with pytest.raises(KaniLogError) as caught:
        parse_kani_log(text)
    return caught.value.reason


def parse_text(text: str) -> KaniLog:
    """Parse a log that the test expects to be accepted."""
    return parse_kani_log(text)


@pytest.mark.parametrize(("version", "name"), CORPUS_CASES)
def test_every_committed_log_yields_its_recorded_facts(
    version: str,
    name: str,
) -> None:
    """Compare each log's banner, format, summary, blocks and checks with the table."""
    expected = EXPECTED[version]["logs"][name]
    if "refused" in expected:
        assert refused(log_text(name, version)) == expected["refused"]
        return
    parsed = parse_text(log_text(name, version))
    assert parsed.banner_version == expected["banner"]
    assert parsed.format == expected["format"]
    assert parsed.unsupported_constructs == tuple(expected["unsupported"])
    if expected["manual"] is None:
        assert parsed.manual is None
    else:
        assert parsed.manual is not None
        assert [
            parsed.manual.verified,
            parsed.manual.failures,
            parsed.manual.total,
            list(parsed.manual.failed),
        ] == [
            expected["manual"]["verified"],
            expected["manual"]["failures"],
            expected["manual"]["total"],
            expected["manual"]["failed"],
        ]
    assert [block_facts(block) for block in parsed.blocks] == expected["blocks"]


def test_every_supported_version_has_a_corpus() -> None:
    """Require a version-named corpus with matching logs for each release."""
    assert tuple(FIXTURES / version for version in SUPPORTED_KANI_VERSIONS) == CORPORA
    for version, entries in EXPECTED.items():
        banners = {
            entry["banner"] for entry in entries["logs"].values() if "banner" in entry
        }
        assert banners == {version}


def test_the_inventory_log_carries_no_verdicts() -> None:
    """Read ``kani list`` output as an inventory rather than a run."""
    parsed = parse_text(log_text("list"))
    assert (parsed.format, parsed.blocks) == ("inventory", ())
    assert parsed.unsupported_constructs == ("TerminatorKind::InlineAsm",)


def test_a_failure_records_where_it_happened() -> None:
    """Keep the source location of each failure, a fact a record cannot invent."""
    block = parse_text(log_text("counterexample")).blocks[0]
    assert [(item.path, item.line) for item in block.failures] == [
        ("src/harnesses.rs", 18),
    ]


def test_the_unwinding_warning_is_recorded_on_the_block_it_names() -> None:
    """Require Kani's unwinding note to survive alongside the failed check."""
    parsed = parse_text(log_text("insufficient_unwind"))
    assert [item.unwinding_reported for item in parsed.blocks] == [True]
    assert [
        item.kind for item in parsed.blocks[0].checks if item.status == "FAILURE"
    ] == ["unwind"]


def test_a_wrong_version_names_the_observed_and_supported_versions() -> None:
    """A log from another release is refused with both versions in the message."""
    text = log_text("verified").replace("0.67.0", "0.66.0", 1)
    reason = refused(text)
    assert "0.66.0" in reason
    assert "0.67.0" in reason


def test_a_duplicated_harness_block_is_refused() -> None:
    """A run that reports the same harness twice is structurally invalid."""
    lines = log_text("verified").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("Checking harness")
    )
    end = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("Verification Time")
    )
    duplicate = lines[start : end + 1]
    manual = next(
        index for index, line in enumerate(lines) if line == "Manual Harness Summary:"
    )
    text = "\n".join(lines[:manual] + duplicate + lines[manual:])
    text = text.replace(
        "Complete - 1 successfully verified harnesses, 0 failures, 1 total.",
        "Complete - 2 successfully verified harnesses, 0 failures, 2 total.",
    )
    assert "reported twice" in refused(text)


def test_a_missing_verdict_is_refused() -> None:
    """A block that ends without its verification result is incomplete."""
    text = "\n".join(
        line
        for line in log_text("verified").splitlines()
        if not line.startswith(
            (
                "SUMMARY:",
                " **",
                "Failed Checks:",
                ' File: "',
                "VERIFICATION:-",
                "Verification Time:",
            ),
        )
    )
    assert "ends without a verification result" in refused(text)


def test_a_missing_manual_summary_is_refused() -> None:
    """A verification run must end with its manual harness summary."""
    lines = log_text("verified").splitlines()
    manual = lines.index("Manual Harness Summary:")
    assert "has no manual harness summary" in refused("\n".join(lines[:manual]))


def test_mismatched_manual_counts_are_refused() -> None:
    """A manual summary must agree with the blocks it summarises."""
    text = log_text("verified").replace(
        "Complete - 1 successfully verified harnesses, 0 failures, 1 total.",
        "Complete - 2 successfully verified harnesses, 0 failures, 2 total.",
    )
    assert "the manual summary claims" in refused(text)


def test_a_non_ascii_harness_name_is_read() -> None:
    """Read a harness whose name is not plain ASCII."""
    text = log_text("verified").replace("harnesses::verified", "harnesses::vérifie")
    assert parse_text(text).blocks[0].name == "harnesses::vérifie"


def test_a_check_without_a_location_is_read() -> None:
    """Accept a check Kani could not attribute to a source location."""
    text = "\n".join(
        line
        for line in log_text("verified").splitlines()
        if not line.strip().startswith("- Location:")
    )
    block = parse_text(text).blocks[0]
    assert [item.kind for item in block.properties] == [
        "assertion",
        "cover",
        "assertion",
    ]


def test_a_check_without_a_numeric_ordinal_is_refused() -> None:
    """Require the ordinal CBMC appends to every property it reports.

    Without it a line that merely begins ``Check 1:`` would be read as a check
    and could end the block's result section early.  The line is not a check,
    so it is an unrecognised line in the block like any other.
    """
    text = log_text("verified").replace(
        "Check 3: harnesses::verified.assertion.1",
        "Check 3: harnesses::verified.assertion",
    )
    assert "unrecognised line in the block" in refused(text)


def test_an_ownerless_check_keeps_its_kind() -> None:
    """Read the kind from two segments, as CBMC prints it without an owner."""
    text = log_text("verified").replace(
        "Check 2: harnesses::verified.cover.1",
        "Check 2: cover.1",
    )
    block = parse_text(text).blocks[0]
    assert [item.kind for item in block.covers] == ["cover"]
    assert block.covers[0].owner is None


def test_a_repeated_check_field_is_refused() -> None:
    """Refuse a check that lists the same field twice rather than guessing."""
    text = log_text("verified").replace(
        "\t - Status: SUCCESS\n\t - Description:",
        "\t - Status: SUCCESS\n\t - Status: SUCCESS\n\t - Description:",
    )
    assert "repeats its Status field" in refused(text)


def test_only_the_framing_quotes_are_removed() -> None:
    """Keep the quotes that belong to the description itself."""
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: ""ready""',
    )
    assert parse_text(text).blocks[0].covers[0].description == '"ready"'


def test_an_undetermined_cover_is_counted() -> None:
    """Reconcile a cover count that names an undetermined cover."""
    text = (
        log_text("verified")
        .replace(
            " ** 1 of 1 cover properties satisfied",
            " ** 0 of 1 cover properties satisfied (1 undetermined)",
        )
        .replace(
            "Check 2: harnesses::verified.cover.1\n\t - Status: SATISFIED",
            "Check 2: harnesses::verified.cover.1\n\t - Status: UNDETERMINED",
        )
    )
    block = parse_text(text).blocks[0]
    assert block.covered_undetermined == 1
    assert [item.status for item in block.covers] == ["UNDETERMINED"]


def test_both_cover_counts_are_reconciled() -> None:
    """Read the comma-separated form Kani prints for two cover statuses."""
    original = (
        "Check 2: harnesses::verified.cover.1\n"
        "\t - Status: SATISFIED\n"
        '\t - Description: "the boundary below saturation is reachable"\n'
        "\t - Location: src/harnesses.rs:11:5 in function harnesses::verified\n"
    )
    undetermined = original.replace("SATISFIED", "UNDETERMINED")
    other = (
        undetermined
        .replace("Check 2:", "Check 4:")
        .replace(".cover.1", ".cover.2")
        .replace("UNDETERMINED", "UNREACHABLE")
    )
    text = (
        log_text("verified")
        .replace(original, f"{undetermined}\n{other}")
        .replace(
            " ** 1 of 1 cover properties satisfied",
            " ** 0 of 2 cover properties satisfied (1 undetermined,1 unreachable)",
        )
    )
    block = parse_text(text).blocks[0]
    assert (block.covered_total, block.covered_unreachable) == (2, 1)
    assert block.covered_undetermined == 1


def test_a_cover_with_a_failure_status_is_refused() -> None:
    """Refuse a cover status Kani converts before it renders a report.

    A listed cover is excluded from the checks whose count is reconciled, so
    without this rule an impossible cover status would pass unexamined.
    """
    text = log_text("verified").replace(
        "Check 2: harnesses::verified.cover.1\n\t - Status: SATISFIED",
        "Check 2: harnesses::verified.cover.1\n\t - Status: FAILURE",
    )
    assert "as FAILURE, which Kani never prints" in refused(text)


def test_an_explicit_zero_other_count_is_reconciled() -> None:
    """Require a printed zero to agree that no check carried that status."""
    text = log_text("insufficient_unwind").replace(
        " ** 1 of 4 failed (3 undetermined)",
        " ** 1 of 4 failed (0 undetermined)",
    )
    assert "counts 0 undetermined results but lists 3" in refused(text)


def test_both_check_status_counts_are_reconciled() -> None:
    """Read the comma-joined form Kani prints for two check statuses."""
    original = (
        "Check 3: core::fmt::Arguments::<'_>::from_str.assertion.1\n"
        "\t - Status: UNDETERMINED\n"
    )
    text = (
        log_text("insufficient_unwind")
        .replace(original, original.replace("UNDETERMINED", "UNREACHABLE"))
        .replace(
            " ** 1 of 4 failed (3 undetermined)",
            " ** 1 of 4 failed (2 undetermined,1 unreachable)",
        )
    )
    block = parse_text(text).blocks[0]
    assert block.other_counts == (("undetermined", 2), ("unreachable", 1))


def test_a_timeout_marker_beside_results_is_refused() -> None:
    """Refuse a timeout marker on a block that went on to report results."""
    lines = log_text("counterexample").splitlines()
    solver = next(index for index, line in enumerate(lines) if "`cbmc " in line)
    text = "\n".join([*lines[: solver + 1], "CBMC timed out.", *lines[solver + 1 :]])
    assert "timeout alongside a completed result" in refused(text)


def test_a_timeout_marker_beside_a_cover_summary_is_refused() -> None:
    """Refuse a timeout block that also carries a cover summary.

    A summary line is only printed once CBMC finished, so its presence beside
    the marker contradicts the marker even when the failed-check count is
    absent.
    """
    text = log_text("multiplication_timeout").replace(
        "VERIFICATION:- FAILED",
        "SUMMARY:\n ** 0 of 1 cover properties satisfied\nVERIFICATION:- FAILED",
    )
    assert "timeout alongside a completed result" in refused(text)


def test_a_completed_result_beside_a_timeout_marker_is_refused() -> None:
    """Refuse a timeout block that also claims a successful verdict."""
    text = log_text("multiplication_timeout").replace(
        "VERIFICATION:- FAILED",
        "VERIFICATION:- SUCCESSFUL",
    )
    assert "timeout alongside a completed result" in refused(text)


def test_a_repeated_cover_status_is_refused() -> None:
    """Refuse a cover line that counts the same status twice."""
    text = log_text("verified").replace(
        " ** 1 of 1 cover properties satisfied",
        " ** 0 of 1 cover properties satisfied (1 unreachable, 1 unreachable)",
    )
    assert "names 'unreachable' twice" in refused(text)


def test_a_block_without_results_is_refused() -> None:
    """Require the ``RESULTS:`` section even when a block has no checks."""
    lines = log_text("verified").splitlines()
    text = "\n".join(
        lines[: lines.index("RESULTS:")] + lines[lines.index("SUMMARY:") :]
    )
    assert "has no results section" in refused(text)


@pytest.mark.usefixtures("default_int_digit_limit")
def test_an_unconvertible_report_integer_is_refused() -> None:
    """Turn a digit run too long to convert into a structural error."""
    text = log_text("verified").replace(
        "Complete - 1 successfully verified",
        f"Complete - {'9' * 4301} successfully verified",
    )
    assert "not a count this parser can read" in refused(text)


def test_an_inventory_with_a_manual_summary_is_refused() -> None:
    """Refuse inventory output that also carries a verification summary."""
    verified = log_text("verified").splitlines()
    manual = verified[verified.index("Manual Harness Summary:") :]
    text = "\n".join([*log_text("list").splitlines(), *manual])
    assert "both an inventory and a verification run" in refused(text)


def test_a_harness_block_after_the_manual_summary_is_refused() -> None:
    """Refuse a block that follows the run's closing summary."""
    lines = log_text("verified").splitlines()
    manual = lines.index("Manual Harness Summary:")
    summary = lines[manual : manual + 2]
    rest = [*lines[:manual], *lines[manual + 2 :]]
    text = "\n".join([rest[0], *summary, *rest[1:]])
    assert "follows the manual harness summary" in refused(text)


def test_a_flag_inside_a_path_does_not_disable_unwinding_checks() -> None:
    """Read solver flags at argument boundaries, not as substrings."""
    inside_a_path = HarnessBlock(
        name="h::x",
        solver_command='[Kani] Running: `cbmc "/tmp/run --no-unwinding-assertions" '
        "--sat-solver cadical`",
    )
    separate = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --no-unwinding-assertions "
        "--sat-solver cadical`",
    )
    assert unwinding_checks_enabled(inside_a_path) is True
    assert unwinding_checks_enabled(separate) is False


def test_a_flag_last_in_the_solver_command_is_still_read() -> None:
    """Read a flag that abuts the echo's closing backtick.

    Kani frames the echoed command in backticks, so the final argument carries
    one unless the framing is removed first. Reading it attached would leave
    the run's most common command - every flag present, the last one closing
    the echo - looking as though checks were enabled.
    """
    flag_last = HarnessBlock(
        name="h::x",
        solver_command=(
            "[Kani] Running: `cbmc --sat-solver cadical --no-unwinding-assertions`"
        ),
    )
    assert unwinding_checks_enabled(flag_last) is False


def test_an_unreadable_solver_command_leaves_unwinding_checks_unknown() -> None:
    """Report an unknown command rather than reading it as enabled checks."""
    unreadable = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --no-unwinding-assertions 'open`",
    )
    assert unwinding_checks_enabled(unreadable) is None


def test_an_unknown_line_inside_a_block_is_refused() -> None:
    """An unrecognised line inside a report is a structural error."""
    lines = log_text("verified").splitlines()
    verdict = next(
        index for index, line in enumerate(lines) if line.startswith("VERIFICATION:-")
    )
    lines.insert(verdict + 1, "this line is not part of a Kani report")
    assert "unrecognised line in the block" in refused("\n".join(lines))


def test_a_failure_without_a_location_is_retained() -> None:
    """Keep the explanation when Kani could not attribute it to a location."""
    text = "\n".join(
        line
        for line in log_text("counterexample").splitlines()
        if not line.startswith(' File: "')
    )
    block = parse_text(text).blocks[0]
    assert [(item.path, item.line) for item in block.failures] == [(None, None)]
    assert block.failures[0].message


def test_a_mixed_inventory_and_verification_log_is_refused() -> None:
    """A log cannot be both an inventory and a verification run."""
    listing = log_text("list").splitlines()
    verified = log_text("verified").splitlines()
    text = "\n".join([*listing, *verified])
    assert "both an inventory and a verification run" in refused(text)


def test_a_front_truncated_tail_of_a_real_log_is_refused() -> None:
    """A log missing its banner cannot be attributed to a Kani release."""
    padding = "\n".join(f"capture chatter {index}" for index in range(10_000))
    text = f"{padding}\n{log_text('verified')}"
    tail = text[-64 * 1024 :]
    assert len(tail.encode("utf-8")) == 64 * 1024
    assert "does not begin with a Kani banner" in refused(tail)


def _chatter_lines() -> st.SearchStrategy[list[str]]:
    """Generate a short list of lines that are not report markers."""
    return st.lists(
        st.text("abcdefghijklmnopqrstuvwxyz 0123456789", max_size=20).map(
            lambda text: f"{CHATTER_LINE} {text}",
        ),
        max_size=4,
    )


def _top_level_insertions(lines: list[str]) -> list[int]:
    """Return indices where chatter sits between report constructs."""
    harness_starts = [
        index for index, line in enumerate(lines) if line.startswith("Checking harness")
    ]
    markers = {*harness_starts[1:]}
    markers.update(
        index for index, line in enumerate(lines) if line == "Manual Harness Summary:"
    )
    return sorted(markers)


def _with_chatter(text: str, chatter: list[str]) -> str:
    """Insert chatter before each top-level report boundary."""
    lines = text.splitlines()
    for index in reversed(_top_level_insertions(lines)):
        lines[index:index] = chatter
    return "\n".join(lines)


@pytest.mark.parametrize("name", ["verified", "default_unwind"])
@settings(max_examples=50, deadline=None)
@given(chatter=_chatter_lines())
def test_unknown_chatter_between_reports_does_not_change_the_result(
    name: str,
    chatter: list[str],
) -> None:
    """Unknown chatter between reports is tolerated and changes no parsed fact."""
    text = log_text(name)
    assert parse_text(_with_chatter(text, chatter)) == parse_text(text)
