"""Command construction and tracing for the opt-in consumer smoke.

These helpers build the public command vectors and run them under `strace`, so
every reuse, refusal and setup claim is observed rather than inferred. They sit
apart from the environment and fixture helpers in
`tests/kani_snapshot_smoke_support.py` to keep both modules readable.
"""

from __future__ import annotations

import dataclasses as dc
import shutil
import typing as typ

from rust_prover_tools.kani_snapshot.producer.setup_trace import parse_exec_trace
from tests.kani_snapshot_smoke_support import Consumer, SmokeContext, read_trace, run

if typ.TYPE_CHECKING:
    import subprocess
    from pathlib import Path

__all__ = (
    "install_command",
    "proof",
    "require_no_setup",
    "require_strace",
    "run_command",
    "traced_run",
)


def install_command(
    prover_tools: list[str], root: Path, assets: Path, cache: Path
) -> list[str]:
    """Return the public install command for one consumer root."""
    return [
        *prover_tools,
        "kani",
        "snapshot",
        "install",
        "--repo-root",
        str(root),
        "--asset-dir",
        str(assets),
        "--cache-dir",
        str(cache),
    ]


def run_command(
    prover_tools: list[str], root: Path, cache: Path, args: tuple[str, ...]
) -> list[str]:
    """Return the public run command with verifier arguments forwarded."""
    return [
        *prover_tools,
        "kani",
        "run",
        "--repo-root",
        str(root),
        "--snapshot-pin",
        str(root / "tools" / "kani" / "snapshot.toml"),
        "--cache-dir",
        str(cache),
        "--",
        *args,
    ]


def require_strace() -> str:
    """Return the strace path, failing when the tool is absent.

    The verification target needs `strace`; without it the reuse, refusal and
    setup claims cannot be observed, so the target refuses to start rather than
    reporting an unproven pass.
    """
    strace = shutil.which("strace")
    assert strace, "strace is required; install it before running this target"
    return strace


def strace_prefix(strace: str, trace: Path) -> list[str]:
    """Return the strace arguments that capture complete argument vectors.

    `-s 4096` raises strace's default 32-byte string limit enough for every
    invocation vector this smoke observes, and `-qq` keeps its own noise out of
    the trace. The parser still refuses an elided argument, so an unexpectedly
    long one fails loudly rather than being silently truncated.
    """
    return [
        strace,
        "-f",
        "-qq",
        "-s",
        "4096",
        "-e",
        "trace=execve",
        "-o",
        str(trace),
    ]


def traced_run(
    command: list[str], trace: Path, context: SmokeContext
) -> subprocess.CompletedProcess[str]:
    """Run one public command under `strace`.

    Parameters
    ----------
    command : list of str
        Command vector to run.
    trace : Path
        File `strace` writes its records to.
    context : SmokeContext
        Working directory, environment and bounds for the child.
    """
    strace = require_strace()
    return run(
        [*strace_prefix(strace, trace), *command], dc.replace(context, check=False)
    )


def proof(
    consumer: Consumer, prover_tools: list[str], crate: str, harness: str
) -> subprocess.CompletedProcess[str]:
    """Run one harness through the public `kani run` command."""
    root = consumer.roots[crate]
    return run(
        run_command(
            prover_tools, root, consumer.cache, ("src/lib.rs", "--harness", harness)
        ),
        SmokeContext(cwd=root, check=False),
    )


def require_no_setup(trace: Path) -> None:
    """Require a trace that did not run the packaged setup again.

    The trace is mandatory: without it the test cannot distinguish reuse from
    an unobserved setup.
    """
    assert trace.is_file(), f"{trace} is missing; strace is required"
    records = parse_exec_trace(read_trace(trace))
    assert not any(
        record.name == "cargo-kani" and "setup" in record.argv[1:] for record in records
    ), "setup ran when the generation should have been reused or refused"
