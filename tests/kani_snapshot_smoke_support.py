"""Shared plumbing for the opt-in consumer smoke.

The smoke's assertions belong in its test module; this module holds the parts
that are not assertions: environment access, one bounded command runner, the
consumer-repository builder and the evidence printers. Keeping them here keeps
each module within the repository's size and argument limits.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import os
import shutil
import subprocess
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_snapshot.models import SnapshotPin
from rust_prover_tools.kani_snapshot.pins import pin_to_toml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "FALSE_HARNESS",
    "FIXTURES",
    "FLOOR_HARNESS",
    "POSITIVE_HARNESSES",
    "PROOF_TIMEOUT",
    "REPO_ROOT",
    "SmokeContext",
    "assets_dir",
    "build_consumer_root",
    "manifest_document",
    "pin_for",
    "prover_tools_command",
    "report",
    "require_environment",
    "run",
    "write_pin",
)

#: The positive harnesses the bounded-collection fixture proves.
POSITIVE_HARNESSES: typ.Final[tuple[str, ...]] = (
    "bounded_push_and_drop_are_exact",
    "raii_release_runs_once",
)

#: The harness whose assertion is false for one reachable input.
FALSE_HARNESS: typ.Final[str] = "deliberately_false_assertion"

#: The floor harness, which only a 1.94-or-newer compiler accepts.
FLOOR_HARNESS: typ.Final[str] = "bounded_adjacent_pairs_are_exact"

#: How long one real proof may take, in seconds.
PROOF_TIMEOUT: typ.Final[int] = 1800

REPO_ROOT: typ.Final[Path] = Path(__file__).resolve().parents[1]
FIXTURES: typ.Final[Path] = REPO_ROOT / "tests" / "fixtures" / "kani_snapshot"

#: Fixture crates the consumer repository carries.
_CRATES: typ.Final[tuple[str, ...]] = (
    "bounded_collection",
    "false_assertion",
    "compiler_floor",
)


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

    Grouping these keeps the runner's signature small and makes a caller state
    the directory and the failure policy at the call site rather than by
    position.

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


def run(command: list[str], context: SmokeContext) -> subprocess.CompletedProcess[str]:
    """Run one public command with combined capture."""
    completed = subprocess.run(
        command,
        cwd=context.cwd,
        env={**os.environ, **(context.env or {})},
        capture_output=True,
        text=True,
        timeout=context.timeout,
        check=False,
    )
    if context.check and completed.returncode != 0:
        pytest.fail(
            f"{' '.join(command)} exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    return completed


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


def build_consumer_root(root: Path, pin: SnapshotPin) -> Path:
    """Build a consumer repository holding the fixture crates and the pin."""
    for crate in _CRATES:
        shutil.copytree(FIXTURES / "proofs" / crate, root / crate)
    write_pin(root, pin)
    report(f"consumer root {root}")
    return root
