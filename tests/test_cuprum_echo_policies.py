"""Contract: a closed reader must not turn an echoed run into a failure.

`prover-tools verus run` echoes proof output, because that output is the
command's user-facing result. Its reader is therefore routinely the other end
of a pipe: `| head`, a pager the user quit, or a CI log collector that has
seen enough. When that reader goes away the next write fails with
`BrokenPipeError`, and what happens next decides whether the proof run
succeeds.

Cuprum 0.1.0 had no policy for this: the echo path propagated the error out of
`run_sync`, so a piped proof run failed even though the proof itself had
succeeded. Cuprum 0.2.0 adds `BrokenPipePolicy`, whose default `STRICT` keeps
the old propagation and whose `BEST_EFFORT` stops echoing the affected stream
while capture, line observation, and child reaping continue. `run_command` opts
into `BEST_EFFORT` for exactly that reason.

Both halves are asserted here, because either one alone is vacuous. Without the
strict test, a future cuprum release could make `BEST_EFFORT` the default and
this suite would keep passing while the opt-in it documents had been removed.
Without the best-effort test, the opt-in could be dropped and only a human with
a pipe in front of the terminal would notice.

The child emits far more output than a pipe buffer holds. That is what makes
the failing write unavoidable rather than a race: a reader that closes after
two lines leaves tens of thousands of lines still to be written, so the echo
path must meet the broken pipe whatever the scheduling.
"""

from __future__ import annotations

import io
import sys
import typing as typ

import pytest
from cuprum import (
    BrokenPipePolicy,
    CommandResult,
    ExecutionContext,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    RunOutputOptions,
    sh,
)

from rust_prover_tools.commands import CommandSpec, run_command

#: Enough output that the pipe buffer cannot hold it all, so the child is
#: still writing when the reader has gone. Small enough to keep the run quick.
_CHILD_LINES = 20_000

#: The strict case's output, in lines of roughly 16 bytes.
#:
#: This must stay below the size at which a raising echo sink stops raising and
#: wedges the run instead: on 0.2.0b2 that boundary is between 152 KiB, which
#: raises, and 153 KiB, which hangs past 200 s. 1,500 lines is about 24 KiB, so
#: `STRICT` fails within a tenth of a second as the test expects. Raising this
#: constant past the threshold would turn a passing test into a hung one.
_STRICT_CHILD_LINES = 1_500


class _ClosedReader(io.StringIO):
    """A destination that refuses every write, as a closed pipe does.

    ``io.StringIO`` rather than ``io.TextIOBase`` is the base that matters
    twice over. It is a genuine text stream, so cuprum's echo path reaches
    ``write`` instead of looking for a ``buffer`` to mirror bytes through; and
    it satisfies ``ExecutionContext.stdout_sink``'s ``IO[str]`` annotation,
    which ``TextIOBase`` does not, so the double needs no suppression to type
    check.
    """

    def __init__(self) -> None:
        """Create a reader that has refused nothing yet."""
        super().__init__()
        self.write_attempts = 0

    def write(self, payload: str, /) -> int:
        """Refuse the write, recording that the echo path reached us."""
        self.write_attempts += 1
        raise BrokenPipeError

    def close(self) -> None:
        """Close without touching the buffer, so teardown cannot re-raise.

        ``StringIO.close`` discards the buffer, but ``io.IOBase.__del__`` calls
        ``close`` at garbage collection, and an unraisable error raised there
        would be attributed to whichever test next dropped a reference.
        """


def _catalogue(*programs: Program) -> ProgramCatalogue:
    """Return a catalogue admitting exactly ``programs`` for one run."""
    project = ProjectSettings(
        name="rust-prover-tools-cuprum-policies",
        programs=programs,
        documentation_locations=("docs/developers-guide.md",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,))


def _run_through_cuprum(
    lines: int,
    *,
    policy: BrokenPipePolicy,
    sink: typ.IO[str],
) -> CommandResult:
    """Run a chatty child under ``policy``, echoing into ``sink``.

    The writable is supplied as ``ExecutionContext.stdout_sink`` rather than
    as ``RunOutputOptions.sink``: the latter takes a presentation adapter whose
    ``open_session`` returns a session, not a stream. Echo then mirrors into
    that stream directly.

    Parameters
    ----------
    lines : int
        How many lines the child prints.
    policy : BrokenPipePolicy
        The broken-pipe policy the run uses.
    sink : IO[str]
        The destination echoed output is mirrored to.

    Returns
    -------
    CommandResult
        The finished run's result, carrying its capture and relay events.
    """
    python = Program(sys.executable)
    catalogue = _catalogue(python)
    source = f"for index in range({lines}): print('proof line', index)"
    command = sh.make(python, catalogue=catalogue)("-c", source)
    return command.run_sync(
        output=RunOutputOptions(echo=True, broken_pipe_policy=policy),
        context=ExecutionContext(stdout_sink=sink),
        timeout=25.0,
    )


def test_a_closed_reader_leaves_the_run_and_its_capture_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_command` finishes when its own stdout has gone away.

    The proof has already succeeded by the time the reader closes, so failing
    the run here would report a proof failure that did not happen. Capture must
    also survive: callers inspect `CommandResult.stdout`, and a run whose echo
    broke still owes them the child's output.
    """
    reader = _ClosedReader()
    monkeypatch.setattr(sys, "stdout", reader)
    source = f"for index in range({_CHILD_LINES}): print('proof line', index)"

    result = run_command(
        CommandSpec((sys.executable, "-c", source), echo=True),
    )

    assert result.exit_code == 0, result.stderr
    assert len(result.stdout.splitlines()) == _CHILD_LINES, (
        "capture must continue after echo is disabled"
    )
    assert reader.write_attempts >= 1, "the echo path must have reached the reader"


def test_the_strict_policy_still_propagates_a_closed_reader() -> None:
    """`STRICT` is the default, which is what makes the opt-in load-bearing.

    A reader could reasonably assume `BEST_EFFORT` is cuprum's default. It is
    not, and this test keeps the neighbouring one from passing on an unchanged
    default rather than on the choice `run_command` makes.
    """
    reader = _ClosedReader()

    with pytest.raises(BrokenPipeError):
        _run_through_cuprum(
            _STRICT_CHILD_LINES,
            policy=BrokenPipePolicy.STRICT,
            sink=reader,
        )

    assert reader.write_attempts >= 1, "the raise must come from the echo path"


def test_best_effort_records_the_fallback_without_raising() -> None:
    """The same closed reader is survivable once the policy opts in.

    `relay_fallbacks` is how a caller can tell that output it saw was
    incomplete, so the entry has to be there and has to name the stream.
    """
    reader = _ClosedReader()

    result = _run_through_cuprum(
        _STRICT_CHILD_LINES,
        policy=BrokenPipePolicy.BEST_EFFORT,
        sink=reader,
    )

    assert result.exit_code == 0, result.stderr
    assert len((result.stdout or "").splitlines()) == _STRICT_CHILD_LINES
    assert [fallback.stream.value for fallback in result.relay_fallbacks] == [
        "stdout"
    ], result.relay_fallbacks
