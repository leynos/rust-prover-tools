"""Support helpers shared by the CLI integration test modules."""

from __future__ import annotations

import os
import sys
import typing as typ

from tests.conftest import FakeVerusBinarySpec

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


#: Enough proof output that the pipe buffer cannot hold it, so the CLI still
#: has output to write when the reader goes away. A reader that stops after five
#: lines is the normal `| head -n 5` case, and a test only has a broken pipe to
#: observe if more output follows the point where the reader left.
PROOF_LINES = 40_000

#: Seconds allowed for the CLI to finish once its reader has gone. The pytest
#: global timeout is 30 seconds, so this leaves room to report a hang as a
#: failed assertion rather than a killed test. The bound covers the reads as
#: well as the wait: a CLI that stops writing marks itself as hung by never
#: closing its own end of the pipe.
CHILD_TIMEOUT = 20


def sanitized_subprocess_env() -> dict[str, str]:
    """Return subprocess environment without GitHub Actions input bindings.

    Returns
    -------
    dict[str, str]
        A copy of the environment with every ``INPUT_`` binding removed.
    """
    return {
        key: value for key, value in os.environ.items() if not key.startswith("INPUT_")
    }


def fake_verus_argv(
    repo_root: Path,
    proof_file: Path,
    verus_bin: Path,
    *,
    extra_args: cabc.Sequence[str] = (),
) -> list[str]:
    """Return the public CLI argument vector for one Verus proof run.

    Parameters
    ----------
    repo_root : Path
        Repository root passed to ``--repo-root``.
    proof_file : Path
        Proof file passed to ``--proof-file``.
    verus_bin : Path
        Fake Verus binary passed to ``--verus-bin``.
    extra_args : collections.abc.Sequence[str]
        Additional options appended after the standard arguments.

    Returns
    -------
    list[str]
        Arguments that invoke the installed CLI through the current interpreter.
    """
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
        *extra_args,
    ]


def chatty_proof_repo(
    repo_root: Path,
    fake_verus_binary_factory: cabc.Callable[..., None],
    *,
    prover_status: int,
) -> tuple[Path, Path]:
    """Create a proof run whose child outlives the reader that started it.

    Parameters
    ----------
    repo_root : Path
        Temporary repository root to populate.
    fake_verus_binary_factory : collections.abc.Callable[..., None]
        Fixture that writes a deterministic fake Verus binary.
    prover_status : int
        Exit status the fake prover returns after streaming its output.

    Returns
    -------
    tuple[Path, Path]
        The proof file and fake Verus binary created in ``repo_root``.

    Notes
    -----
    The child writes ``PROOF_LINES`` lines before exiting with
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
                f'while [ "$i" -lt {PROOF_LINES} ]; do\n'
                '  printf "proof line %s\\n" "$i"\n'
                "  i=$((i+1))\n"
                "done\n"
                f"exit {prover_status}"
            ),
        ),
    )
    return proof_file, verus_bin
