"""Prove the echoed solver command is read at Kani's own boundaries."""

from __future__ import annotations

from rust_prover_tools.kani_log import (
    HarnessBlock,
    unwinding_checks_enabled,
)
from rust_prover_tools.unittests.kani_log_parse_support import (
    log_text,
    parse_text,
    refused,
)


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


def test_a_solver_echo_without_its_closing_backtick_is_unknown() -> None:
    """Refuse a truncated echo rather than reading a cut-off flag as absent."""
    truncated = HarnessBlock(
        name="h::x",
        solver_command="[Kani] Running: `cbmc --no-unwinding-assert",
    )

    assert unwinding_checks_enabled(truncated) is None
