"""Fixtures, pins and the consumer tree for the opt-in smoke.

This module holds the smoke's *data*: the harness names each fixture crate
declares, the paths to the fixtures, the reviewed pin built from a produced
manifest, and the consumer tree that pairs one proof root per crate with a
shared cache. Running commands lives in `kani_snapshot_smoke_runner` and
reading their output in `kani_snapshot_smoke_evidence`; the assertions belong
in the test module.

The split keeps one reason to change per module: a new fixture crate touches
here, a new bound on the runner touches the runner, and a new log shape
touches the evidence readers.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import os
import shutil
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
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotRuntime,
    snapshot_layout,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import toolchain_root
from tests.kani_snapshot_smoke_evidence import (
    MAX_TRACE_BYTES,
    generation_from_output,
    read_trace,
    report,
)
from tests.kani_snapshot_smoke_runner import (
    MAX_CAPTURE_BYTES,
    PROOF_TIMEOUT,
    SmokeContext,
    run,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "FALSE_HARNESS",
    "FIXTURES",
    "FLOOR_HARNESS",
    "MAX_CAPTURE_BYTES",
    "MAX_TRACE_BYTES",
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

REPO_ROOT: typ.Final[Path] = Path(__file__).resolve().parents[1]
FIXTURES: typ.Final[Path] = REPO_ROOT / "tests" / "fixtures" / "kani_snapshot"

#: Fixture crates the consumer smoke exercises.
CRATES: typ.Final[tuple[str, ...]] = (
    "bounded_collection",
    "false_assertion",
    "compiler_floor",
    *UPSTREAM_BUNDLE_HARNESSES,
)


def require_environment(name: str) -> str:
    """Return one required environment variable or fail naming it."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must name an existing path")
    return value


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
