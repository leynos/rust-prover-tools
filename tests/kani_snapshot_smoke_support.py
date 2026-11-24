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
from cuprum.context import UnsetType

from rust_prover_tools.kani_exec_routing import runtime_search_path
from rust_prover_tools.kani_snapshot.identity import execution_environment
from rust_prover_tools.kani_snapshot.install import check_active_snapshot
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.models import SnapshotPin
from rust_prover_tools.kani_snapshot.pins import load_pin, pin_to_toml
from rust_prover_tools.kani_snapshot.producer.setup_trace import MAX_TRACE_BYTES
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotRuntime,
    snapshot_layout,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import toolchain_root

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
    "UPSTREAM_BUNDLE_HARNESSES",
    "Consumer",
    "SmokeContext",
    "assets_dir",
    "build_root",
    "generation_from_output",
    "manifest_document",
    "pin_for",
    "prover_tools_command",
    "read_trace",
    "report",
    "require_environment",
    "run",
    "snapshot_environment",
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

#: Upstream's three bundle-test crates and every harness each one declares.
#:
#: Kani's `readable_name` strips the local crate prefix before the driver prints
#: a harness, so a harness inside a module is reported as `kani_tests::test_sum`
#: rather than as a bare function name. A harness from another module therefore
#: cannot satisfy these expectations.
UPSTREAM_BUNDLE_HARNESSES: typ.Final[dict[str, tuple[str, ...]]] = {
    "simple-lib": (
        "kani_tests::test_sum",
        "pair::kani_tests::test_one_plus_two",
    ),
    "build-rs-works": ("check",),
    "simple-kissat": ("check_concat",),
}

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
    *UPSTREAM_BUNDLE_HARNESSES,
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
    replace_env : bool
        When true, `env` is the child's whole environment rather than an
        overlay on the caller's.
    """

    cwd: Path
    env: cabc.Mapping[str, str] | None = None
    timeout: int = PROOF_TIMEOUT
    check: bool = True
    replace_env: bool = False


def _capture(handle: typ.BinaryIO) -> tuple[str, bool]:
    """Return a captured stream and whether it exceeded the ceiling."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    truncated = size > MAX_CAPTURE_BYTES
    handle.seek(max(0, size - MAX_CAPTURE_BYTES))
    return handle.read().decode("utf-8", errors="replace"), truncated


def run(command: list[str], context: SmokeContext) -> subprocess.CompletedProcess[str]:
    """Run one public command with bounded capture.

    The child's streams go to temporary files, and only their tails are
    loaded. A proof log is therefore never held whole in memory, and the
    validator ceilings apply to text the caller already had to bound.
    """
    environment = (
        dict(context.env or {})
        if context.replace_env
        else {**os.environ, **(context.env or {})}
    )
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            command,
            cwd=context.cwd,
            env=environment,
            stdout=out,
            stderr=err,
            timeout=context.timeout,
            check=False,
        )
        stdout, stdout_truncated = _capture(out)
        stderr, stderr_truncated = _capture(err)
    if stdout_truncated or stderr_truncated:
        pytest.fail(
            f"{' '.join(command)} produced more than {MAX_CAPTURE_BYTES} bytes; "
            "an incomplete transcript cannot be validated"
        )
    result = subprocess.CompletedProcess(command, completed.returncode, stdout, stderr)
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
    shutil.copytree(FIXTURES / "proofs" / crate, root, dirs_exist_ok=True)
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
        One proof root per fixture crate, each holding its sources and the pin.
    cache : Path
        Cache shared by every root, so one install serves every proof.
    """

    roots: dict[str, Path]
    cache: Path

    def runtime(self, root: Path) -> SnapshotRuntime:
        """Return the active generation's runtime for one proof root's pin."""
        pin = load_pin(root / "tools" / "kani" / "snapshot.toml")
        identity = check_active_snapshot(InstallOptions(pin=pin, cache_home=self.cache))
        return SnapshotRuntime(
            layout=snapshot_layout(
                pin, cache_home=self.cache, generation=identity.generation.name
            ),
            toolchain=identity.toolchain,
            backend_version=identity.backend_version,
        )


def snapshot_environment(runtime: SnapshotRuntime) -> dict[str, str]:
    """Return the exact environment a snapshot proof runs with."""
    environment = {
        name: value
        for name, value in execution_environment(runtime, os.environ).items()
        if not isinstance(value, UnsetType)
    }
    environment["LD_LIBRARY_PATH"] = runtime_search_path(
        toolchain_root(runtime) / "lib",
        os.environ.get("LD_LIBRARY_PATH", ""),
    )
    return environment
