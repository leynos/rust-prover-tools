"""Prove the Kani parser refuses report structure no run can produce."""

from __future__ import annotations

from rust_prover_tools.unittests.kani_log_parse_support import (
    log_text,
    refused,
)


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
