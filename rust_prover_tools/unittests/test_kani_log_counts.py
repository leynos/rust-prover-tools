"""Prove the Kani parser reconciles each cover and status count."""

from __future__ import annotations

from rust_prover_tools.unittests.kani_log_parse_support import (
    log_text,
    parse_text,
    refused,
)


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
