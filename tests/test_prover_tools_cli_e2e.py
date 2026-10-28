"""End-to-end tests for externally observable CLI workflows."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import typing as typ

import pytest

from tests.conftest import FakeVerusBinarySpec

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


#: Enough proof output that the pipe buffer cannot hold it, so the CLI still
#: has output to write when the reader goes away. A reader that stops after five
#: lines is the normal `| head -n 5` case, and this test only has a broken pipe
#: to observe if more output follows the point where the reader left.
_PROOF_LINES = 4_000

#: Lines the test reader consumes before closing, mirroring `| head -n 5`.
_READER_LINES = 5

#: Seconds allowed for the CLI to finish once its reader has gone. The pytest
#: global timeout is 30 seconds, so this leaves room to report a hang as a
#: failed assertion rather than a killed test.
_CHILD_TIMEOUT = 20


def sanitized_subprocess_env() -> dict[str, str]:
    """Return subprocess environment without GitHub Actions input bindings."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("INPUT_")
    }


def fake_verus_argv(
    repo_root: Path,
    proof_file: Path,
    verus_bin: Path,
) -> list[str]:
    """Return the public CLI argument vector for one Verus proof run."""
    return [
        sys.executable,
        "-m",
        "rust_prover_tools.cli",
        "verus",
        "run",
        "--repo-root",
        str(repo_root),
        "--proof-file",
        str(proof_file),
        "--verus-bin",
        str(verus_bin),
        "--no-ensure-toolchain",
    ]


def chatty_proof_repo(
    repo_root: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    *,
    prover_status: int,
) -> tuple[Path, Path]:
    """Create a proof run whose child outlives the reader that started it.

    The child writes ``_PROOF_LINES`` lines before exiting with
    ``prover_status``. That is far more than a pipe buffer holds, so closing the
    reader early reaches the writer as a broken pipe instead of being absorbed by
    kernel buffering.
    """
    version_file = repo_root / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    proof_file = repo_root / "proof.rs"
    proof_file.write_text("proof\n", encoding="utf-8")
    verus_bin = repo_root / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(
            proof_command=(
                "i=0\n"
                f'while [ "$i" -lt {_PROOF_LINES} ]; do\n'
                '  printf "proof line %s\\n" "$i"\n'
                "  i=$((i+1))\n"
                "done\n"
                f"exit {prover_status}"
            ),
        ),
    )
    return proof_file, verus_bin


def test_verus_run_cli_accepts_indented_toolchain_output(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """The public CLI accepts official indented Verus toolchain output."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    proof_file = tmp_path / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(
            version_stdout=(
                "Verus 0.2025.01.01\\n"
                "  Toolchain: nightly-2025-01-01-x86_64-unknown-linux-gnu "
                "(overridden by environment variable RUSTUP_TOOLCHAIN)\\n"
            ),
            proof_command="printf 'verified %s\\n' \"$1\"",
        ),
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--repo-root",
            str(tmp_path),
            "--proof-file",
            str(proof_file),
            "--verus-bin",
            str(verus_bin),
            "--no-ensure-toolchain",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert "verified" in completed.stdout, (
        f"expected Verus proof output; stdout: {completed.stdout}; "
        f"stderr: {completed.stderr}"
    )


@pytest.mark.parametrize(
    "proof_env_var",
    ["VERUS_PROOF_FILE", "INPUT_PROOF_FILE"],
)
def test_verus_run_env_proof_file_is_not_split_on_path_separator(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    proof_env_var: str,
) -> None:
    """A legacy env-var proof path keeps path separators inside one filename."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("2025.01.01\n", encoding="utf-8")
    # Use the platform's own separator so the directory name stays legal on
    # Windows while still exercising the split Cyclopts would otherwise apply.
    proof_dir = tmp_path / f"proofs{os.pathsep}separator"
    proof_dir.mkdir()
    proof_file = proof_dir / "proof.rs"
    proof_file.write_text("proof", encoding="utf-8")
    verus_bin = tmp_path / "bin" / "verus"
    fake_verus_binary_factory(
        verus_bin,
        spec=FakeVerusBinarySpec(proof_command="printf 'verified %s\\n' \"$1\""),
    )

    environment = sanitized_subprocess_env()
    environment[proof_env_var] = str(proof_file)
    environment["VERUS_BIN"] = str(verus_bin)
    environment["INPUT_REPO_ROOT"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "run",
            "--no-ensure-toolchain",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, (
        f"expected zero returncode, got {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    assert f"verified {proof_file}" in completed.stdout, (
        f"expected one proof invocation; stdout: {completed.stdout}; "
        f"stderr: {completed.stderr}"
    )


def test_verus_run_streams_every_line_when_the_reader_stays(
    tmp_path: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
) -> None:
    """A reader that stays receives the whole output and the prover's status.

    This is the no-broken-pipe reference for the piped cases: streaming must not
    become conditional, and the failing status must survive the ordinary path.
    """
    proof_file, verus_bin = chatty_proof_repo(
        tmp_path,
        fake_verus_binary_factory,
        prover_status=3,
    )

    completed = subprocess.run(
        fake_verus_argv(tmp_path, proof_file, verus_bin),
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
        timeout=_CHILD_TIMEOUT,
    )

    assert completed.returncode == 3, (
        f"expected the prover's status 3, got {completed.returncode}; "
        f"stderr: {completed.stderr}"
    )
    assert f"proof line {_PROOF_LINES - 1}\n" in completed.stdout, (
        "expected every streamed line, including the last; "
        f"stdout ended with: {completed.stdout[-200:]!r}"
    )


def read_until_the_reader_goes_away(
    argv: list[str],
    *,
    reader_lines: int,
) -> tuple[int, list[str]]:
    """Run ``argv`` and close its standard output after ``reader_lines`` lines.

    The pipe is the only channel between the CLI and this reader, so closing it
    while the child still has output to write is exactly the `| head -n 5`
    situation. The child's status is returned unchanged: this helper must not
    interpret it, because interpreting it is what the tests are asking about.
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
        consumed = [
            line for _, line in zip(range(reader_lines), process.stdout, strict=False)
        ]
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
        try:
            return process.wait(timeout=_CHILD_TIMEOUT), consumed
        except subprocess.TimeoutExpired:  # pragma: no cover - diagnosed as hang
            process.kill()
            msg = f"the CLI did not exit after its reader closed; stderr: {stderr!r}"
            raise AssertionError(msg) from None


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
