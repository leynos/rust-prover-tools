"""Shared plumbing for the opt-in consumer smoke.

The smoke's assertions belong in its test module; this module holds the parts
that are not assertions: environment access, one bounded command runner, the
consumer-root builder and the evidence readers. Keeping them here keeps each
module within the repository's size and argument limits, and keeping every read
bounded keeps a hostile log from sizing the process.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_snapshot.models import SnapshotPin
from rust_prover_tools.kani_snapshot.pins import pin_to_toml
from rust_prover_tools.kani_snapshot.producer.setup_trace import (
    MAX_TRACE_BYTES,
    parse_exec_trace,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "FALSE_HARNESS",
    "FIXTURES",
    "FLOOR_HARNESS",
    "MAX_CAPTURE_BYTES",
    "POSITIVE_HARNESSES",
    "PROOF_TIMEOUT",
    "REPO_ROOT",
    "Consumer",
    "SmokeContext",
    "assets_dir",
    "build_root",
    "generation_from_output",
    "install_command",
    "manifest_document",
    "pin_for",
    "proof",
    "prover_tools_command",
    "read_trace",
    "report",
    "require_environment",
    "require_no_setup",
    "run",
    "run_command",
    "traced_run",
    "write_pin",
)

#: The positive harnesses the bounded-collection fixture proves.
POSITIVE_HARNESSES: typ.Final[tuple[str, ...]] = (
    "bounded_push_contents_are_exact",
    "raii_release_runs_once",
)

#: The harness whose assertion is false for one reachable input.
FALSE_HARNESS: typ.Final[str] = "deliberately_false_assertion"

#: The floor harness, which only a 1.94-or-newer compiler accepts.
FLOOR_HARNESS: typ.Final[str] = "bounded_adjacent_pairs_are_exact"

#: How long one real proof may take, in seconds.
PROOF_TIMEOUT: typ.Final[int] = 1800

#: How much of a command's output is retained for diagnostics. The launcher
#: mirrors output live; only this tail is loaded into memory.
MAX_CAPTURE_BYTES: typ.Final[int] = 1024 * 1024

REPO_ROOT: typ.Final[Path] = Path(__file__).resolve().parents[1]
FIXTURES: typ.Final[Path] = REPO_ROOT / "tests" / "fixtures" / "kani_snapshot"

#: Fixture crates the consumer smoke exercises.
CRATES: typ.Final[tuple[str, ...]] = (
    "bounded_collection",
    "false_assertion",
    "compiler_floor",
)

_GENERATION: typ.Final[re.Pattern[str]] = re.compile(r"\bat (?P<path>\S+)$")


def require_environment(name: str) -> str:
    """Return one required environment variable or fail naming it."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must name an existing path")
    return value


def report(note: str) -> None:
    """Print one bounded evidence note, so a passing run is legible."""
    print(f"[kani-smoke] {note}")


@dc.dataclass(frozen=True, slots=True)
class SmokeContext:
    """One command invocation's working directory and bounds.

    Attributes
    ----------
    cwd : Path
        Working directory for the child.
    env : collections.abc.Mapping of str to str, optional
        Variables added to the inherited environment.
    timeout : int
        Seconds the child may run.
    check : bool
        Whether a non-zero exit fails the test immediately.
    """

    cwd: Path
    env: cabc.Mapping[str, str] | None = None
    timeout: int = PROOF_TIMEOUT
    check: bool = True


def _tail_text(handle: typ.BinaryIO) -> str:
    """Return at most `MAX_CAPTURE_BYTES` from the end of a captured stream."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - MAX_CAPTURE_BYTES))
    return handle.read().decode("utf-8", errors="replace")


def run(command: list[str], context: SmokeContext) -> subprocess.CompletedProcess[str]:
    """Run one public command with bounded capture.

    The child's streams go to temporary files, and only their tails are
    loaded. A proof log is therefore never held whole in memory, and the
    validator ceilings apply to text the caller already had to bound.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            command,
            cwd=context.cwd,
            env={**os.environ, **(context.env or {})},
            stdout=out,
            stderr=err,
            timeout=context.timeout,
            check=False,
        )
        result = subprocess.CompletedProcess(
            command,
            completed.returncode,
            _tail_text(out),
            _tail_text(err),
        )
    if context.check and result.returncode != 0:
        pytest.fail(
            f"{' '.join(command)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result


def assets_dir() -> Path:
    """Return the assembled release directory."""
    return Path(require_environment("KANI_SNAPSHOT_SMOKE_ASSET_DIR")).resolve()


def manifest_document(assets: Path) -> dict[str, object]:
    """Return the produced manifest, refusing a document of the wrong shape."""
    document = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the manifest must be a JSON object"
    return typ.cast("dict[str, object]", document)


def pin_for(assets: Path, manifest: cabc.Mapping[str, object]) -> SnapshotPin:
    """Return the reviewed pin that names exactly these manifest bytes."""
    digest = hashlib.sha256((assets / "manifest.json").read_bytes()).hexdigest()
    return SnapshotPin(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag=typ.cast("str", manifest["tag"]),
        upstream_commit=typ.cast("str", manifest["upstream_commit"]),
        builder_commit=typ.cast("str", manifest["builder_commit"]),
        target=typ.cast("str", manifest["target"]),
        manifest_sha256=digest,
    )


def prover_tools_command() -> list[str]:
    """Return the command vector that runs the installed console script."""
    return [require_environment("KANI_SNAPSHOT_SMOKE_PROVER_TOOLS")]


def write_pin(root: Path, pin: SnapshotPin) -> None:
    """Write the reviewed pin into a consumer repository."""
    (root / "tools" / "kani").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "kani" / "snapshot.toml").write_bytes(pin_to_toml(pin))


def build_root(root: Path, pin: SnapshotPin, crate: str) -> Path:
    """Build a proof root holding one fixture source file and the pin.

    The public `kani run` command executes the standalone launcher from the
    repository root, so each proof needs its own root with the source file the
    launcher compiles.
    """
    (root / "src").mkdir(parents=True)
    shutil.copyfile(
        FIXTURES / "proofs" / crate / "src" / "lib.rs",
        root / "src" / "lib.rs",
    )
    write_pin(root, pin)
    report(f"proof root {root}")
    return root


def read_trace(path: Path) -> str:
    """Return a trace, refusing one larger than the documented ceiling."""
    with path.open("rb") as handle:
        data = handle.read(MAX_TRACE_BYTES + 1)
    assert len(data) <= MAX_TRACE_BYTES, (
        f"{path} exceeds the {MAX_TRACE_BYTES}-byte trace ceiling"
    )
    return data.decode("utf-8", errors="replace")


def generation_from_output(text: str) -> str | None:
    """Return the generation path an install or check line names, if any."""
    for line in reversed(text.splitlines()):
        if line.startswith("snapshot "):
            match = _GENERATION.search(line)
            if match is not None:
                return match.group("path")
    return None


@dc.dataclass(frozen=True, slots=True)
class Consumer:
    """One consumer tree: a proof root per crate and a shared cache.

    Attributes
    ----------
    roots : dict of str to Path
        One proof root per fixture crate, each holding `src/lib.rs` and the pin.
    cache : Path
        Cache shared by every root, so one install serves every proof.
    """

    roots: dict[str, Path]
    cache: Path


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


def traced_run(
    command: list[str], cwd: Path, trace: Path
) -> subprocess.CompletedProcess[str]:
    """Run one public command under `strace`, or plainly without it."""
    strace = shutil.which("strace")
    if strace is None:
        return run(command, SmokeContext(cwd=cwd, check=False))
    return run(
        [strace, "-f", "-e", "trace=execve", "-o", str(trace), *command],
        SmokeContext(cwd=cwd, check=False),
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
    """Require a trace that did not run the packaged setup again."""
    if not trace.is_file():
        return
    records = parse_exec_trace(read_trace(trace))
    assert not any(
        record.name == "cargo-kani" and "setup" in record.argv[1:] for record in records
    ), "setup ran when the generation should have been reused or refused"
