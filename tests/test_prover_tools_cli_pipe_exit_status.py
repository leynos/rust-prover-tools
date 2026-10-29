"""End-to-end tests for the exit status seen through a closed pipe."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import typing as typ

import pytest

from tests.test_prover_tools_cli_e2e import (
    _CHILD_TIMEOUT,
    _READER_LINES,
    chatty_proof_repo,
    fake_verus_argv,
    read_until_the_reader_goes_away,
    sanitized_subprocess_env,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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
        reader_lines=_READER_LINES,
    )

    assert consumed == [f"proof line {index}\n" for index in range(_READER_LINES)], (
        f"expected the first {_READER_LINES} streamed lines, got {consumed!r}"
    )
    assert status == prover_status, (
        f"expected the prover's status {prover_status} through the closed pipe, "
        f"got {status}"
    )


def test_verus_run_reports_the_prover_status_through_a_tee_pipeline(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """`| tee log | head` preserves the status a CI log capture reads.

    `tee` is the wrapper most likely to sit between the CLI and a reader that
    stops early, and it forwards the broken pipe to the CLI. The pipeline is run
    by `bash` with `pipefail` so that the exit status of the CLI itself, not the
    last command, is what the test observes.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the tee pipeline")
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )
    log_file = tmp_path / "pipeline.log"
    pipeline = (
        '"$@" 2>/dev/null'
        f" | tee {shlex.quote(str(log_file))}"
        f" | head -n {_READER_LINES} > /dev/null"
    )
    script = f"""
set -o pipefail
{pipeline}
printf '%s\\n' "${{PIPESTATUS[0]}}"
"""

    completed = subprocess.run(
        [
            bash,
            "-c",
            script,
            "pipeline",
            *fake_verus_argv(tmp_path, proof_file, verus_bin),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=_CHILD_TIMEOUT,
    )

    assert completed.returncode == 0, (
        f"the pipeline helper failed: stdout={completed.stdout!r}; "
        f"stderr={completed.stderr!r}"
    )
    reported = completed.stdout.strip().splitlines()
    assert reported == ["3"], (
        f"expected the CLI to report the prover's status 3 through tee, "
        f"got {reported!r}; stderr: {completed.stderr!r}"
    )
    assert log_file.read_text(encoding="utf-8").startswith("proof line 0\n"), (
        "expected tee to receive the streamed proof output"
    )


def test_verus_run_reports_the_prover_status_through_a_pager_that_quits(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """A reader that quits mid-stream cannot replace the prover's status.

    Quitting a pager closes the receiving end while the CLI still has output to
    write, which is the same broken pipe `| head` produces but without the
    reader consuming a fixed count first. `sed 5q` stands in for a pager that
    stops reading after the fifth line and exits.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the pager pipeline")
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )
    script = f"""
set -o pipefail
"$@" 2>/dev/null | sed '{_READER_LINES}q' > /dev/null
printf '%s\\n' "${{PIPESTATUS[0]}}"
"""

    completed = subprocess.run(
        [
            bash,
            "-c",
            script,
            "pipeline",
            *fake_verus_argv(tmp_path, proof_file, verus_bin),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=_CHILD_TIMEOUT,
    )

    assert completed.returncode == 0, (
        f"the pipeline helper failed: stdout={completed.stdout!r}; "
        f"stderr={completed.stderr!r}"
    )
    reported = completed.stdout.strip().splitlines()
    assert reported == ["3"], (
        f"expected the CLI to report the prover's status 3 after the pager "
        f"quit, got {reported!r}; stderr: {completed.stderr!r}"
    )


def test_verus_run_reports_the_prover_status_through_a_combined_pipe(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """`2>&1 | head` cannot replace the prover's status either.

    Merging the streams makes the CLI write diagnostics into a pipe the reader
    abandons. The shutdown flush of both streams then meets the closed pipe, so
    the status must survive a failure on either descriptor, not just on
    `sys.stdout`.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the combined pipeline")
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )
    script = f"""
set -o pipefail
"$@" 2>&1 | head -n {_READER_LINES} > /dev/null
printf '%s\\n' "${{PIPESTATUS[0]}}"
"""

    completed = subprocess.run(
        [
            bash,
            "-c",
            script,
            "pipeline",
            *fake_verus_argv(tmp_path, proof_file, verus_bin),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=_CHILD_TIMEOUT,
    )

    assert completed.returncode == 0, (
        f"the pipeline helper failed: stdout={completed.stdout!r}; "
        f"stderr={completed.stderr!r}"
    )
    reported = completed.stdout.strip().splitlines()
    assert reported == ["3"], (
        f"expected the CLI to report the prover's status 3 through a combined "
        f"pipe, got {reported!r}; stderr: {completed.stderr!r}"
    )
