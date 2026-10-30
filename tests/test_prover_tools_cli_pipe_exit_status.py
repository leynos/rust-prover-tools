"""End-to-end tests for the exit status seen through a closed pipe."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import typing as typ

import pytest

from tests.support import (
    CHILD_TIMEOUT,
    chatty_proof_repo,
    fake_verus_argv,
    sanitized_subprocess_env,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


#: Lines the test reader consumes before closing, mirroring `| head -n 5`.
READER_LINES = 5


@pytest.mark.parametrize("prover_status", [0, 3])
def test_verus_run_reports_the_prover_status_through_a_closed_pipe(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    prover_status: int,
) -> None:
    """A reader that closes early cannot replace the prover's exit status.

    CPython exits 120 when the interpreter's own shutdown flush of `sys.stdout`
    fails, which is what a pipe closed by `| head` causes. That overrides
    whatever `sys.exit` was given, so a wrapper reading the status through the
    pipe cannot tell a passing proof from a failing one. Both rows of the
    reported symptom are asserted, because only the failing row would otherwise
    be indistinguishable from a broken run.
    """
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=prover_status,
    )

    status, consumed = read_until_the_reader_goes_away(
        fake_verus_argv(tmp_path, proof_file, verus_bin),
        reader_lines=READER_LINES,
    )

    assert consumed == [f"proof line {index}\n" for index in range(READER_LINES)], (
        f"expected the first {READER_LINES} streamed lines, got {consumed!r}"
    )
    assert status == prover_status, (
        f"expected the prover's status {prover_status} through the closed pipe, "
        f"got {status}"
    )


def read_until_the_reader_goes_away(
    argv: list[str],
    *,
    reader_lines: int,
) -> tuple[int, list[str]]:
    """Run ``argv`` and close its standard output after ``reader_lines`` lines.

    Parameters
    ----------
    argv : list[str]
        Command and arguments to run.
    reader_lines : int
        Number of output lines to consume before closing the pipe.

    Returns
    -------
    tuple[int, list[str]]
        The child's exit status, unchanged, and the lines this reader consumed.

    Notes
    -----
    The pipe is the only channel between the CLI and this reader, so closing it
    while the child still has output to write is exactly the `| head -n 5`
    situation. The status is returned uninterpreted because interpreting it is
    what the tests are asking about.
    """
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=sanitized_subprocess_env(),
    ) as process:
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            msg = "subprocess pipes were not created"
            raise AssertionError(msg)
        expired = threading.Event()

        def abandon_the_run() -> None:
            # Killing the child is what unblocks a read that would otherwise
            # wait forever for output the CLI never writes.
            expired.set()
            process.kill()

        watchdog = threading.Timer(CHILD_TIMEOUT, abandon_the_run)
        watchdog.daemon = True
        watchdog.start()
        consumed: list[str] = []
        stderr = ""
        try:
            consumed = [
                line
                for _, line in zip(range(reader_lines), process.stdout, strict=False)
            ]
            process.stdout.close()
            stderr = process.stderr.read()
            process.stderr.close()
            status = process.wait()
        finally:
            watchdog.cancel()
        if expired.is_set():  # pragma: no cover - diagnosed as hang
            msg = (
                f"the CLI neither finished within {CHILD_TIMEOUT}s of its reader "
                f"closing nor stopped writing; stderr: {stderr!r}"
            )
            raise AssertionError(msg) from None
        return status, consumed


def _run_pipeline(
    pipeline: str,
    argv: list[str],
) -> subprocess.CompletedProcess[str]:
    """Run ``pipeline`` under bash and print the first stage's status.

    Bash sets ``PIPESTATUS`` after every pipeline, independent of ``pipefail``,
    so the printed first entry is the CLI's own status rather than the last
    command's.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the pipeline")
    script = f"""{pipeline}
printf '%s\\n' "${{PIPESTATUS[0]}}"
"""
    return subprocess.run(
        [bash, "-c", script, "pipeline", *argv],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=CHILD_TIMEOUT,
    )


def _reported_status(
    completed: subprocess.CompletedProcess[str],
    *,
    context: str,
) -> list[str]:
    """Return the status lines printed by ``_run_pipeline``, checking its run."""
    assert completed.returncode == 0, (
        f"the pipeline helper failed {context}: stdout={completed.stdout!r}; "
        f"stderr={completed.stderr!r}"
    )
    return completed.stdout.strip().splitlines()


def test_verus_run_reports_the_prover_status_through_a_tee_pipeline(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """`| tee log | head` preserves the status a CI log capture reads.

    `tee` is the wrapper most likely to sit between the CLI and a reader that
    stops early, and it forwards the broken pipe to the CLI. `PIPESTATUS[0]`
    reports the CLI's own status; the pipeline's last-command status would say
    nothing about the proof.
    """
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )
    log_file = tmp_path / "pipeline.log"
    pipeline = (
        '"$@" 2>/dev/null'
        f" | tee {shlex.quote(str(log_file))}"
        f" | head -n {READER_LINES} > /dev/null"
    )

    completed = _run_pipeline(
        pipeline,
        fake_verus_argv(tmp_path, proof_file, verus_bin),
    )

    reported = _reported_status(completed, context="through tee")
    assert reported == ["3"], (
        f"expected the CLI to report the prover's status 3 through tee, "
        f"got {reported!r}; stderr: {completed.stderr!r}"
    )
    assert log_file.read_text(encoding="utf-8").startswith("proof line 0\n"), (
        "expected tee to receive the streamed proof output"
    )


@pytest.mark.parametrize(
    "pipeline",
    [
        f"\"$@\" 2>/dev/null | sed '{READER_LINES}q' > /dev/null",
        f'"$@" 2>&1 | head -n {READER_LINES} > /dev/null',
    ],
    ids=["pager-quits", "combined-stdout-and-stderr"],
)
def test_verus_run_reports_the_prover_status_through_a_closing_reader(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    pipeline: str,
) -> None:
    """A reader that goes away cannot replace the prover's status.

    `sed '{READER_LINES}q'` stands in for a pager that quits mid-stream, closing
    the receiving end without consuming a fixed count first. `2>&1 | head`
    exercises a combined redirect, where both streams share one dead descriptor
    and the shutdown flush of either could replace the status.
    """
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )

    completed = _run_pipeline(
        pipeline,
        fake_verus_argv(tmp_path, proof_file, verus_bin),
    )

    reported = _reported_status(completed, context="after the reader went away")
    assert reported == ["3"], (
        f"expected the CLI to report the prover's status 3 after the reader "
        f"went away, got {reported!r}; stderr: {completed.stderr!r}"
    )
