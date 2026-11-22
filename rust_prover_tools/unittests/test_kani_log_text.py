"""Prove description and explanation text stays inside its frame."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.unittests.kani_log_parse_support import (
    CHATTER_LINE,
    log_text,
    parse_text,
    refused,
)


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
    # Cut the encoded form, as a byte-oriented capture tool does; the cut lands
    # in the ASCII padding, so it cannot split a multibyte character.
    tail = text.encode("utf-8")[-64 * 1024 :].decode("utf-8")
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


_DESCRIPTION_BOUNDARIES = (
    pytest.param(
        '- Description: "first"\n\nRESULTS:\nand second"',
        'first"\n\nRESULTS:\nand second',
        id="results-marker",
    ),
    pytest.param(
        '- Description: "first"\n\nSUMMARY:\nand second"',
        'first"\n\nSUMMARY:\nand second',
        id="summary-marker",
    ),
    pytest.param(
        '- Description: "first"\n\nManual Harness Summary:\nand second"',
        'first"\n\nManual Harness Summary:\nand second',
        id="manual-summary-marker",
    ),
    pytest.param(
        '- Description: "first"\n\t - Location: src/harnesses.rs:1:1 in f\nlast"',
        'first"\n\t - Location: src/harnesses.rs:1:1 in f\nlast',
        id="field-shaped-continuation",
    ),
    pytest.param(
        '- Description: "first"\n\n\t - Location: src/harnesses.rs:1:1 in f\nlast"',
        'first"\n\n\t - Location: src/harnesses.rs:1:1 in f\nlast',
        id="field-shaped-continuation-after-blank",
    ),
    pytest.param(
        '- Description: "first"\n\t - Location: src/harnesses.rs:1:1 in f\n'
        'SUMMARY:\nand second"',
        'first"\n\t - Location: src/harnesses.rs:1:1 in f\nSUMMARY:\nand second',
        id="field-then-summary-marker",
    ),
    pytest.param(
        '- Description: "first"\n\n\t - Location: src/harnesses.rs:1:1 in f\n'
        'SUMMARY:\nand second"',
        'first"\n\n\t - Location: src/harnesses.rs:1:1 in f\nSUMMARY:\nand second',
        id="blank-then-field-then-summary-marker",
    ),
)


@pytest.mark.parametrize(("replacement", "expected"), _DESCRIPTION_BOUNDARIES)
def test_a_report_shaped_line_inside_a_description_is_read_in_full(
    replacement: str,
    expected: str,
) -> None:
    """Keep report-shaped text inside a description when it is content.

    Kani escapes nothing, so a description may print a report marker or a
    field-shaped line on its own line.  Each is a boundary only when the
    structure that really follows a marker or field is present; here more
    description follows, so the frame stays open.
    """
    text = log_text("verified").replace(
        '- Description: "the boundary below saturation is reachable"',
        replacement,
    )
    block = parse_text(text).blocks[0]
    assert block.covers[0].description == expected


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


def test_a_non_cover_check_cannot_carry_a_cover_status() -> None:
    """Refuse a status Kani applies only to cover properties."""
    text = log_text("verified").replace(
        "Check 3: harnesses::verified.assertion.1\n\t - Status: SUCCESS",
        "Check 3: harnesses::verified.assertion.1\n\t - Status: SATISFIED",
    )
    assert "is not a cover but carries the cover status" in refused(text)
