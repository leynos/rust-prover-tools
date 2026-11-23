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


def test_a_fail_fast_summary_naming_two_failures_is_refused() -> None:
    """Refuse a summary that counts two failures as a fail-fast result.

    Kani's driver passes exactly one failed result to its final-summary
    routine when ``--fail-fast`` stops the run, so a summary naming two failed
    blocks matches no shape either mode can print.
    """
    text = log_text("fail_fast")
    start = text.index("Checking harness")
    end = text.index("\n", text.index("Verification Time:", start)) + 1
    block = text[start:end]
    renamed = block.replace("seeded_wrapping_mutant", "other_mutant")
    success = _captured_block("harnesses::verified")
    text = text.replace("Checking harness", success + renamed + "Checking harness", 1)
    text = text.replace(
        "Verification failed for - harnesses::seeded_wrapping_mutant\n",
        "Verification failed for - harnesses::other_mutant\n"
        "Verification failed for - harnesses::seeded_wrapping_mutant\n",
    )
    text = text.replace(
        "Complete - 0 successfully verified harnesses, 1 failures, 1 total.",
        "Complete - 0 successfully verified harnesses, 2 failures, 2 total.",
    )

    assert "the manual summary claims" in refused(text)


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


def test_a_repeated_description_at_a_report_boundary_is_refused() -> None:
    """Refuse a second Description field instead of absorbing it as text.

    The first description ends with a quote, so the boundary scan decides
    whether the next field-shaped line is continuation text.  A real field run
    followed by the summary is a duplicate the entry reader must reject.
    """
    text = log_text("verified").replace(
        "\t - Description: \"This is a placeholder message; Kani doesn't support "
        'message formatted at runtime"\n\t - Location: src/harnesses.rs:7:5',
        '\t - Description: "first"\n\t - Description: "second"\n'
        "\t - Location: src/harnesses.rs:7:5",
    )

    assert "repeats its Description field" in refused(text)


def test_a_description_may_print_a_summary_and_count_shaped_text() -> None:
    """Keep an embedded SUMMARY: and `` **`` line inside the description.

    The renderer prints the total before any other summary line, so a marker
    whose next line merely begins with `` **`` is description content.
    """
    text = log_text("verified").replace(
        "\t - Description: \"This is a placeholder message; Kani doesn't support "
        'message formatted at runtime"',
        '\t - Description: "first"\n\nSUMMARY:\n ** example\nlast"',
    )
    block = parse_text(text).blocks[0]

    assert any("SUMMARY:" in item.description for item in block.properties), (
        "the embedded summary text must stay inside the description"
    )


def test_a_detached_check_row_is_refused() -> None:
    """Refuse a check row written after the block it belongs to has ended."""
    lines = log_text("verified").splitlines()
    index = next(
        position
        for position, line in enumerate(lines)
        if line.startswith("Verification Time:")
    )
    lines[index + 1 : index + 1] = [
        "Check 1: harnesses::detached.assertion.1",
        "\t - Status: FAILURE",
        '\t - Description: "detached"',
    ]

    assert "a report line outside a harness block" in refused("\n".join(lines))


def test_results_after_its_summary_are_refused() -> None:
    """Refuse a results section that arrives after the summary explains it."""
    lines = log_text("verified").splitlines()
    start = lines.index("RESULTS:")
    end = lines.index("SUMMARY:")
    results = lines[start:end]
    verdict = next(
        position
        for position, line in enumerate(lines)
        if line.startswith("VERIFICATION:-")
    )
    without = lines[:start] + lines[end:]
    insertion = verdict - len(results)
    text = "\n".join(without[: insertion + 1] + results + without[insertion + 1 :])

    assert "has results after its summary" in refused(text)


def test_a_second_results_section_is_refused() -> None:
    """Refuse a repeated results heading rather than reading its checks twice."""
    text = log_text("verified").replace("RESULTS:\n", "RESULTS:\nRESULTS:\n", 1)

    assert "more than one RESULTS: section" in refused(text)


def test_a_successful_verdict_over_a_failed_check_is_refused() -> None:
    """Refuse a clean verdict unless Kani's expected-panic note explains it."""
    text = (
        log_text("verified")
        .replace(
            "Check 3: harnesses::verified.assertion.1\n\t - Status: SUCCESS",
            "Check 3: harnesses::verified.assertion.1\n\t - Status: FAILURE",
        )
        .replace(" ** 0 of 2 failed", " ** 1 of 2 failed")
        .replace(
            "\nVERIFICATION:- SUCCESSFUL",
            "\nFailed Checks: This is a placeholder message; Kani doesn't "
            "support message formatted at runtime\n"
            ' File: "src/harnesses.rs", line 7, in harnesses::verified\n'
            "\nVERIFICATION:- SUCCESSFUL",
        )
    )

    assert "reports SUCCESSFUL while a check failed" in refused(text)


def test_an_unreachable_status_requires_its_subtotal() -> None:
    """Refuse a positive status whose printed subtotal is missing."""
    text = log_text("verified").replace(" (1 unreachable)", "")

    assert "lists 1 unreachable results but has no unreachable count" in refused(text)


def test_an_undetermined_cover_requires_its_subtotal() -> None:
    """Refuse a cover subtotal the renderer would have printed but the log omits."""
    text = (
        log_text("verified")
        .replace("Status: SATISFIED", "Status: UNDETERMINED")
        .replace(
            " ** 1 of 1 cover properties satisfied",
            " ** 0 of 1 cover properties satisfied",
        )
    )

    assert "lists 1 undetermined covers but has no undetermined count" in refused(text)


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
