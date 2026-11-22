"""Prove the Kani parser reconciles each block against its report."""

from __future__ import annotations

import pytest

from rust_prover_tools.unittests.kani_log_parse_support import (
    log_text,
    parse_text,
    refused,
)


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


def _captured_block(name: str) -> str:
    """Return one corpus harness block, through its verification time."""
    text = log_text("verified")
    start = text.index(f"Checking harness {name}...")
    end = text.index("Verification Time:", start)
    return text[start : text.index("\n", end) + 1]


def _fail_fast_after_a_success(summary: str | None = None) -> str:
    """Return the fail-fast capture with one successful block before it."""
    text = log_text("fail_fast")
    if summary is not None:
        text = text.replace(
            "Complete - 0 successfully verified harnesses, 1 failures, 1 total.",
            summary,
        )
    block = _captured_block("harnesses::verified")
    return text.replace("Checking harness", block + "Checking harness", 1)


def test_a_fail_fast_summary_counts_only_the_failing_harness() -> None:
    """Accept the shape ``--fail-fast`` produces after an earlier success.

    The driver stops at the first failure and passes only that result to its
    final-summary routine, so a successful block printed before it is absent
    from the counts.  The completed blocks still have to reconcile.
    """
    parsed = parse_text(_fail_fast_after_a_success())

    assert [block.name for block in parsed.blocks] == [
        "harnesses::verified",
        "harnesses::seeded_wrapping_mutant",
    ]
    assert parsed.manual is not None
    assert (parsed.manual.verified, parsed.manual.failures, parsed.manual.total) == (
        0,
        1,
        1,
    )


def test_a_fail_fast_summary_that_loses_a_failure_is_refused() -> None:
    """Refuse a summary whose counts match no shape the driver prints."""
    text = _fail_fast_after_a_success(
        "Complete - 0 successfully verified harnesses, 1 failures, 2 total.",
    )

    assert "the manual summary claims" in refused(text)


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


def test_an_unwinding_note_beside_a_timeout_is_refused() -> None:
    """Refuse a timeout block carrying the unwinding-failure note."""
    text = log_text("multiplication_timeout").replace(
        "CBMC timed out.",
        "[Kani] info: Verification output shows one or more unwinding failures.\n"
        "CBMC timed out.",
        1,
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


def test_a_report_line_outside_a_block_is_refused() -> None:
    """Refuse report markers that no harness block owns."""
    text = log_text("verified").replace(
        "Manual Harness Summary:",
        "RESULTS:\nVERIFICATION:- FAILED\nManual Harness Summary:",
        1,
    )

    assert "outside a harness block" in refused(text)


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


def test_a_second_banner_inside_a_block_is_refused() -> None:
    """Refuse a banner swallowed by the solver transcript of a live block."""
    text = log_text("verified").replace(
        "RESULTS:",
        "Kani Rust Verifier 0.67.0 (cargo plugin)\nRESULTS:",
        1,
    )

    assert "second Kani banner" in refused(text)
