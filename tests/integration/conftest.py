"""Fixtures for the opt-in Kani snapshot consumer smoke.

The smoke's assertions stay in its test module; these fixtures prepare the
release directory, the reviewed pin and one proof root per fixture crate. The
environment is still gated by the test module's `KANI_SNAPSHOT_SMOKE` skip, so
ordinary `make test` never runs a real install.
"""

from __future__ import annotations

import shutil
import typing as typ

import pytest

from tests.kani_snapshot_smoke_support import (
    CRATES,
    Consumer,
    SmokeContext,
    assets_dir,
    build_root,
    install_command,
    manifest_document,
    pin_for,
    prover_tools_command,
    run,
)

if typ.TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotPin

#: The release files every produced asset directory must carry.
_REQUIRED_ASSETS: typ.Final[tuple[str, ...]] = (
    "manifest.json",
    "manifest.sigstore.jsonl",
    "SHA256SUMS",
    "inventory.json",
)


@pytest.fixture(scope="module")
def assets() -> Path:
    """Return the release directory and require its complete file set."""
    directory = assets_dir()
    missing = sorted(
        name for name in _REQUIRED_ASSETS if not (directory / name).is_file()
    )
    assert not missing, f"the release directory lacks {missing}"
    return directory


@pytest.fixture(scope="module")
def manifest(assets: Path) -> dict[str, object]:
    """Return the produced manifest."""
    return manifest_document(assets)


@pytest.fixture(scope="module")
def pin(assets: Path, manifest: dict[str, object]) -> SnapshotPin:
    """Return the reviewed pin that names exactly these manifest bytes."""
    return pin_for(assets, manifest)


@pytest.fixture(scope="module")
def prover_tools() -> list[str]:
    """Return the command vector that runs the installed console script."""
    return prover_tools_command()


@pytest.fixture(scope="module")
def consumer(tmp_path_factory: pytest.TempPathFactory, pin: SnapshotPin) -> Consumer:
    """Build one proof root per fixture crate and one shared cache."""
    base = tmp_path_factory.mktemp("consumer")
    roots = {crate: build_root(base / crate, pin, crate) for crate in CRATES}
    return Consumer(roots=roots, cache=base / "cache")


@pytest.fixture(scope="module")
def installed(
    consumer: Consumer, prover_tools: list[str], assets: Path
) -> subprocess.CompletedProcess[str]:
    """Install the produced snapshot through the public command, cold.

    Setup is traced only when `strace` is available; the descendant-process
    test records the gap rather than claiming the check ran.
    """
    root = consumer.roots["bounded_collection"]
    command = install_command(prover_tools, root, assets, consumer.cache)
    strace = shutil.which("strace")
    if strace is None:
        return run(command, SmokeContext(cwd=root))
    return run(
        [strace, "-f", "-e", "trace=execve", "-o", str(root / "setup.trace"), *command],
        SmokeContext(cwd=root),
    )
