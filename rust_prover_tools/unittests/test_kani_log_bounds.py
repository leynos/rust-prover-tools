"""Hold the shared parser's retained collections to their record budgets."""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log import KaniLogError, parse_kani_log
from rust_prover_tools.kani_log.checks import MAX_DESCRIPTION_CHARS
from rust_prover_tools.kani_log.extract import MAX_PARSER_RECORDS
from rust_prover_tools.kani_log.reports import MAX_BLOCK_RECORDS
from rust_prover_tools.kani_log.unsupported import MAX_UNSUPPORTED

_BANNER = "Kani Rust Verifier 0.67.0 (cargo plugin)"


def _passing_block(name: str) -> list[str]:
    """Return one complete successful harness block."""
    return [
        f"Checking harness {name}...",
        "RESULTS:",
        f"Check 1: {name}.assertion.1",
        "\t - Status: SUCCESS",
        '\t - Description: "x"',
        f"\t - Location: src/x.rs:1:1 in function {name}",
        "",
        "SUMMARY:",
        " ** 0 of 1 failed",
        "",
        "VERIFICATION:- SUCCESSFUL",
        "Verification Time: 0.1s",
        "",
    ]


def _failing_block(name: str, explanations: int) -> list[str]:
    """Return one failing block carrying `explanations` failure lines."""
    return [
        f"Checking harness {name}...",
        "RESULTS:",
        f"Check 1: {name}.assertion.1",
        "\t - Status: FAILURE",
        '\t - Description: "x"',
        f"\t - Location: src/x.rs:1:1 in function {name}",
        "",
        "SUMMARY:",
        " ** 1 of 1 failed",
        "",
        "Failed Checks: x",
        *(f"Failed Checks: explanation {index}" for index in range(explanations - 1)),
        "VERIFICATION:- FAILED",
        "Verification Time: 0.1s",
        "",
    ]


def _refusal(*lines: str) -> str:
    """Return the parser's refusal reason for a synthetic log."""
    with pytest.raises(KaniLogError) as caught:
        parse_kani_log("\n".join(lines))
    return caught.value.reason


def test_more_than_the_block_budget_is_refused() -> None:
    """A log naming more harness blocks than the budget is refused."""
    blocks = [
        line
        for index in range(MAX_PARSER_RECORDS + 1)
        for line in _passing_block(f"h{index}")
    ]

    assert "harness blocks" in _refusal(_BANNER, *blocks)


def test_more_than_the_block_failure_budget_is_refused() -> None:
    """One block cannot retain more failed checks than the budget."""
    block = _failing_block("h", MAX_BLOCK_RECORDS + 1)

    assert "failures" in _refusal(_BANNER, *block)


def test_more_than_the_manual_failure_budget_is_refused() -> None:
    """The manual summary cannot name more failures than the budget."""
    block = _failing_block("h", 1)
    manual = [
        "Manual Harness Summary:",
        "Verification failed for - h",
        *(f"Verification failed for - h{index}" for index in range(MAX_BLOCK_RECORDS)),
        "Complete - 0 successfully verified harnesses, 1 failures, 1 total.",
    ]

    assert "failed harnesses" in _refusal(_BANNER, *block, *manual)


def test_more_than_the_unsupported_budget_is_refused() -> None:
    """A pre-flight list cannot retain more entries than the budget."""
    entries = [
        (" " * 13) + "- TerminatorKind::InlineAsm (1)"
        for _ in range(MAX_UNSUPPORTED + 1)
    ]
    lines = [
        _BANNER,
        "warning: Found the following unsupported constructs:",
        *entries,
        "",
        "Verification will fail if one or more of these constructs is reachable.",
    ]

    assert "unsupported list" in _refusal(*lines)


def _description_report(description: str) -> str:
    """Return a complete report whose only check carries `description`."""
    return "\n".join([
        _BANNER,
        "Checking harness h0...",
        "RESULTS:",
        "Check 1: h0.assertion.1",
        "\t - Status: SUCCESS",
        f'\t - Description: "{description}"',
        "\t - Location: src/x.rs:1:1 in function h0",
        "",
        "SUMMARY:",
        " ** 0 of 1 failed",
        "",
        "VERIFICATION:- SUCCESSFUL",
        "Verification Time: 0.1s",
        "",
        "Manual Harness Summary:",
        "Complete - 1 successfully verified harnesses, 0 failures, 1 total.",
    ])


def test_a_single_line_description_past_the_budget_is_refused() -> None:
    """The first description line counts against the budget, not only its tail."""
    text = _description_report("x" * (MAX_DESCRIPTION_CHARS + 1))

    with pytest.raises(KaniLogError, match="character budget"):
        parse_kani_log(text)


def test_a_single_line_description_at_the_budget_is_accepted() -> None:
    """A rendered description exactly at the budget is retained.

    The budget counts the value as Kani prints it, framing quotes included,
    which is the same span the continuation accounting measures.
    """
    payload = "x" * (MAX_DESCRIPTION_CHARS - 2)
    text = _description_report(payload)

    block = parse_kani_log(text).blocks[0]
    assert block.properties[0].description == payload
