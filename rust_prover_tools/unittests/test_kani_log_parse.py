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
    where = f"{version}/{name}.log"
    assert parsed.banner_version == expected["banner"], f"banner of {where}"
    assert parsed.format == expected["format"], f"format of {where}"
    assert parsed.unsupported_constructs == tuple(expected["unsupported"]), (
        f"unsupported constructs of {where}"
    )
    if expected["manual"] is None:
        assert parsed.manual is None, f"unexpected manual summary in {where}"
    else:
        assert parsed.manual is not None, f"missing manual summary in {where}"
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
        ], f"manual summary of {where}"
    assert [block_facts(block) for block in parsed.blocks] == expected["blocks"], (
        f"blocks of {where}"
    )


def test_every_supported_version_has_a_corpus() -> None:
    """Require exactly one corpus directory per supported release.

    Comparing the directory names with the supported list catches both a
    missing release and a stale directory left behind after a bump, which a
    comparison against a list built the same way could never show.
    """
    on_disk = {item.name for item in FIXTURES.iterdir() if item.is_dir()}
    assert on_disk == set(SUPPORTED_KANI_VERSIONS), (
        "corpus directories must match the supported releases"
    )
    for version, entries in EXPECTED.items():
        banners = {
            entry["banner"] for entry in entries["logs"].values() if "banner" in entry
        }
        assert banners == {version}, f"every {version} log must carry its own banner"


@pytest.mark.parametrize("version", SUPPORTED_KANI_VERSIONS)
def test_every_recorded_log_exists(version: str) -> None:
    """Require the recorded logs and the files on disk to be the same set.

    Comparing in both directions catches a log added without an expectation as
    well as an expectation left behind by a deleted log; either would otherwise
    be invisible to the parametrized corpus tests.
    """
    recorded = set(EXPECTED[version]["logs"])
    on_disk = {
        path.stem for path in (FIXTURES / version).iterdir() if path.suffix == ".log"
    }
    assert on_disk == recorded, (
        f"{version} logs on disk {sorted(on_disk)} do not match the recorded "
        f"set {sorted(recorded)}"
    )


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


def test_a_duplicated_check_identifier_is_refused() -> None:
    """A block that reports one check identifier twice is structurally invalid.

    The identifier is unique only within its harness, so a repeat in one block
    would make the same property count twice.  The same identifier in another
    block stays ordinary, which the committed corpus already exercises.
    """
    lines = log_text("verified").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("Check 2: ")
    )
    duplicate = lines[start : start + 4]
    text = "\n".join(lines[: start + 4] + duplicate + lines[start + 4 :])
    assert "reports check 'harnesses::verified.cover.1' twice" in refused(text)


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


def test_a_description_spanning_lines_is_read_in_full() -> None:
    """Join a value Kani printed over several lines into one description.

    Kani does not escape a description, so a newline inside a cover message
    starts a line with no framing of its own.  Reading only the first line
    would leave the continuation to be rejected as an unrecognized line in the
    block, refusing an otherwise complete log.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: "the boundary below\nsaturation is reachable"',
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == "the boundary below\nsaturation is reachable"


def test_a_description_whose_line_ends_with_a_quote_is_read_in_full() -> None:
    """Keep reading past an embedded quote that happens to end a line.

    Kani escapes nothing inside a description, so a value holding both a quote
    and a newline prints a line that ends with the very character the frame is
    recognised by.  Closing the description there would leave the rest to be
    rejected as an unrecognised line in the block, refusing a complete log.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: ""ready"\nnext"',
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == '"ready"\nnext'


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


def test_a_second_kani_banner_is_refused() -> None:
    """Refuse a capture that holds more than one Kani invocation.

    A second banner means the file is two runs concatenated.  The first run's
    facts describe only itself, so returning them would silently ignore the
    second invocation - including any failure it reported after its banner.
    """
    text = log_text("verified") + (
        "\nKani Rust Verifier 0.67.0 (cargo plugin)\n"
        "error: could not compile `kani-corpus` (lib) due to 1 previous error\n"
    )
    assert "second Kani banner" in refused(text)


def test_a_failed_verdict_carrying_the_panic_note_is_refused() -> None:
    """Refuse a verdict Kani never prints with the expected-panic note.

    Kani prints that note only when every failed property is an assertion, and
    that is exactly the combination it calls successful.  A failed verdict
    carrying the note describes a report the pinned release cannot produce, and
    accepting it would let the note excuse a failure Kani itself did not.
    """
    text = log_text("expected_panic").replace(
        "VERIFICATION:- SUCCESSFUL (encountered one or more panics as expected)",
        "VERIFICATION:- FAILED (encountered one or more panics as expected)",
    )
    assert "expected-panic note" in refused(text)


def test_a_flag_inside_a_path_does_not_disable_unwinding_checks() -> None:
    """Read solver flags at argument boundaries, not as substrings.

    The wrapped path ends the echo, so its contents are unambiguously one
    argument and the flag spelled inside it was never passed as an option.
    """
    inside_a_path = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --sat-solver cadical "
        '"/tmp/run --no-unwinding-assertions"`',
    )
    separate = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --no-unwinding-assertions "
        "--sat-solver cadical`",
    )
    assert unwinding_checks_enabled(inside_a_path) is True
    assert unwinding_checks_enabled(separate) is False


def test_a_wrapped_argument_followed_by_text_leaves_checks_unknown() -> None:
    """Refuse a wrapped argument whose closing quote could be content.

    ``"a b" --flag`` is what two arguments render as, and also what one
    argument holding ``a b" --flag`` renders as.  Kani escapes nothing, so the
    flag's presence cannot be decided from the text.
    """
    ambiguous = HarnessBlock(
        name="h::x",
        solver_command='[Kani] Running: `cbmc "/tmp/a b" --sat-solver cadical`',
    )
    assert unwinding_checks_enabled(ambiguous) is None


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


def test_an_apostrophe_in_the_echo_does_not_hide_the_flag() -> None:
    """Split the echo at Kani's boundaries rather than as shell text.

    Kani wraps an argument in double quotes only when it holds a space and
    escapes nothing inside it, so an apostrophe is an ordinary character.
    Reading the echo as shell text would let one apostrophe open a quoted
    region that swallows the flag sitting between two paths.
    """
    ambiguous = HarnessBlock(
        name="h::x",
        solver_command=(
            "[Kani] Running: `cbmc /tmp/solver' --no-unwinding-assertions "
            "/tmp/model'.goto`"
        ),
    )
    assert unwinding_checks_enabled(ambiguous) is False


def test_an_unreadable_solver_command_leaves_unwinding_checks_unknown() -> None:
    """Report an unknown command rather than reading it as enabled checks."""
    unreadable = HarnessBlock(
        name="h::x",
        solver_command='[Kani] Running: `cbmc --no-unwinding-assertions "/tmp/run`',
    )
    assert unwinding_checks_enabled(unreadable) is None


def test_a_quote_inside_a_wrapped_argument_leaves_checks_unknown() -> None:
    """Refuse a rendering whose argument boundaries cannot be recovered.

    Kani wraps only a whole argument, so a quote that is followed by more text
    before the next space means the rendering cannot be split into the words
    the solver was actually given.
    """
    ambiguous = HarnessBlock(
        name="h::x",
        solver_command='[Kani] Running: `cbmc "/tmp/a"b c" --sat-solver cadical`',
    )
    assert unwinding_checks_enabled(ambiguous) is None


def test_two_conflicting_solver_echoes_are_refused() -> None:
    """Refuse a block whose echoes disagree about the solver's settings.

    The run mode reads the echoed command to learn whether the unwinding
    assertions were generated.  Two different echoes cannot both describe the
    single invocation that produced the block, so keeping whichever came last
    would make the reported profile depend on line order.
    """
    lines = log_text("verified").splitlines()
    echo = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("[Kani] Running: `cbmc ")
    )
    lines.insert(
        echo + 1,
        "[Kani] Running: `cbmc --no-unwinding-assertions --sat-solver cadical`",
    )
    assert "echoes two different solver commands" in refused("\n".join(lines))


def test_a_repeated_identical_solver_echo_is_accepted() -> None:
    """Accept a block that echoes the same solver command twice."""
    lines = log_text("verified").splitlines()
    echo = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("[Kani] Running: `cbmc ")
    )
    lines.insert(echo + 1, lines[echo])
    assert parse_text("\n".join(lines)).blocks[0].solver_command == lines[echo]


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


def test_a_multiline_failure_explanation_is_read_in_full() -> None:
    """Consume the lines a newline in the failed description splits it across.

    Kani prints the failed property's description twice: quoted in its check
    and raw beneath ``Failed Checks:``.  It escapes neither, so a description
    holding a newline prints over two lines in both places.  Reading only the
    first line of the explanation would leave the continuation to be refused
    as an unrecognised summary line.
    """
    placeholder = (
        "This is a placeholder message; Kani doesn't support message "
        "formatted at runtime"
    )
    broken = "This is a placeholder message;\nKani doesn't support..."
    text = (
        log_text("counterexample")
        .replace(
            f'- Description: "{placeholder}"',
            '- Description: "This is a placeholder message;\nKani doesn\'t support..."',
        )
        .replace(f"Failed Checks: {placeholder}", f"Failed Checks: {broken}")
    )
    block = parse_text(text).blocks[0]
    assert block.failures[0].message == broken


def test_a_failure_explanation_contradicting_its_check_is_refused() -> None:
    """Refuse a summary that explains a different description than it listed.

    Both renderings come from one description, so a continuation line that
    disagrees with the check's own description means the report is not
    self-consistent.  Reading it anyway would attribute a failure to words the
    results never reported.
    """
    placeholder = (
        "This is a placeholder message; Kani doesn't support message "
        "formatted at runtime"
    )
    text = (
        log_text("counterexample")
        .replace(
            f'- Description: "{placeholder}"',
            '- Description: "This is a placeholder message;\nsecond line"',
        )
        .replace(
            f"Failed Checks: {placeholder}",
            "Failed Checks: This is a placeholder message;\nsomething else",
        )
    )
    assert "disagrees with the check it explains" in refused(text)


def test_a_mixed_inventory_and_verification_log_is_refused() -> None:
    """A log cannot be both an inventory and a verification run."""
    listing = log_text("list").splitlines()
    # Drop the second banner: it would name a second invocation, which the
    # parser refuses for that reason before it can see the mixed shapes.
    verified = log_text("verified").splitlines()[1:]
    text = "\n".join([*listing, *verified])
    assert "both an inventory and a verification run" in refused(text)


def test_a_front_truncated_tail_of_a_real_log_is_refused() -> None:
    """A log missing its banner cannot be attributed to a Kani release.

    The padding goes *after* the banner so the tail a front-truncating capture
    keeps holds no banner at all.  Putting it before the banner would leave the
    banner in the middle of the kept tail, and a parser that accepted leading
    chatter would then pass a test meant to prove the banner is required.
    """
    banner, *rest = log_text("verified").splitlines()
    padding = [f"capture chatter {index}" for index in range(10_000)]
    text = "\n".join([banner, *padding, *rest])
    tail = text[-64 * 1024 :]
    assert len(tail.encode("utf-8")) == 64 * 1024
    assert not tail.startswith(banner)
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


def test_a_description_with_an_embedded_quote_then_a_blank_line_is_read_in_full() -> (
    None
):
    """A blank line inside a description does not close the check entry.

    The description below renders ``""ready"`` on its first line and a blank
    line before the continuation.  The blank is content: the entry boundary is
    the next report construct, so the frame must stay open across it.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: ""ready"\n\nand continued"',
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == '"ready"\n\nand continued'


def test_a_description_holding_a_results_marker_is_read_in_full() -> None:
    """Keep a report-shaped word inside a description when it is content.

    Kani escapes nothing, so a description may print ``RESULTS:`` on its own
    line.  A real results section is followed by a ``Check N:`` entry; this one
    is followed by more description, so the frame stays open.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: "first"\n\nRESULTS:\nand second"',
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == 'first"\n\nRESULTS:\nand second'


def test_an_expected_panic_note_without_a_failed_check_is_refused() -> None:
    """Refuse a report Kani never prints: a note with nothing to excuse.

    The driver prints the note only after excusing a failed property as the
    declared panic, so a block whose checks all passed cannot carry it.
    """
    text = log_text("expected_panic").replace(
        "Check 1: harnesses::expected_panic.assertion.1\n\t - Status: FAILURE",
        "Check 1: harnesses::expected_panic.assertion.1\n\t - Status: SUCCESS",
    )

    assert "expected-panic note but no check failed" in refused(text)


def test_a_description_holding_a_summary_marker_is_read_in_full() -> None:
    """Keep ``SUMMARY:`` as description content unless a summary follows.

    A real summary begins with its `` ** …`` counts or the verdict, so a marker
    followed by ordinary text stays inside the description.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        '- Description: "first"\n\nSUMMARY:\nand second"',
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == 'first"\n\nSUMMARY:\nand second'


def test_a_non_cover_check_cannot_carry_a_cover_status() -> None:
    """Refuse a status Kani applies only to cover properties."""
    text = log_text("verified").replace(
        "Check 3: harnesses::verified.assertion.1\n\t - Status: SUCCESS",
        "Check 3: harnesses::verified.assertion.1\n\t - Status: SATISFIED",
    )
    assert "is not a cover but carries the cover status" in refused(text)


def test_a_solver_echo_without_its_closing_backtick_is_unknown() -> None:
    """Refuse a truncated echo rather than reading a cut-off flag as absent."""
    truncated = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --no-unwinding-assert",
    )

    assert unwinding_checks_enabled(truncated) is None
