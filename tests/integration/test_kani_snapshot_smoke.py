"""Opt-in end-to-end verification of one produced Kani snapshot.

This module is the consumer half of EP-M5. It takes a release directory that a
non-publishing hosted run assembled, installs it through the public commands in
a fresh environment, and requires the evidence D6 and V8 name: the effective
compiler at or above the floor, the exact named positive harnesses, a
deliberately false assertion that failed verification rather than compilation,
a compiler-floor harness that only Rust 1.94 accepts, the Rust 1.93 control,
a warm-cache repeat, and a corrupted payload and changed identity refused
before execution.

It is skipped unless `KANI_SNAPSHOT_SMOKE` is set, because it downloads a
pinned toolchain, runs setup and executes real proofs. `make
kani-snapshot-verify` supplies the environment; ordinary `make test` never
reaches it.

The non-assertion plumbing lives in `tests/kani_snapshot_smoke_support.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_snapshot.models import SnapshotPin
from rust_prover_tools.kani_snapshot.producer.compiler_floor import (
    COMPILER_FLOOR,
    require_compiler_floor,
    require_floor_harness_verified,
    require_unstable_feature_rejected,
)
from rust_prover_tools.kani_snapshot.producer.proof_report import (
    require_named_harnesses,
)
from rust_prover_tools.kani_snapshot.producer.setup_trace import (
    require_prebuilt_setup,
)
from tests.kani_snapshot_smoke_support import (
    FALSE_HARNESS,
    FIXTURES,
    FLOOR_HARNESS,
    POSITIVE_HARNESSES,
    SmokeContext,
    assets_dir,
    build_consumer_root,
    manifest_document,
    pin_for,
    prover_tools_command,
    report,
    require_environment,
    run,
)

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("KANI_SNAPSHOT_SMOKE"),
        reason="set KANI_SNAPSHOT_SMOKE=1 through make kani-snapshot-verify",
    ),
    pytest.mark.timeout(1800),
]

#: The release files every produced asset directory must carry.
_REQUIRED_ASSETS: typ.Final[tuple[str, ...]] = (
    "manifest.json",
    "manifest.sigstore.jsonl",
    "SHA256SUMS",
    "inventory.json",
)

type Installed = subprocess.CompletedProcess[str]


def _install_command(prover_tools: list[str], root: Path, assets: Path) -> list[str]:
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
    ]


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
def consumer_root(tmp_path_factory: pytest.TempPathFactory, pin: SnapshotPin) -> Path:
    """Build a consumer repository holding the fixtures and the reviewed pin."""
    root = tmp_path_factory.mktemp("consumer")
    return build_consumer_root(root, pin)


@pytest.fixture(scope="module")
def installed(consumer_root: Path, prover_tools: list[str], assets: Path) -> Installed:
    """Install the produced snapshot through the public command, cold.

    Setup is traced when `strace` is available, so the descendant-process
    policy is discharged from the same run rather than a separate one.
    """
    strace = os.environ.get("KANI_SNAPSHOT_SMOKE_STRACE") or "strace"
    command = _install_command(prover_tools, consumer_root, assets)
    trace = consumer_root / "setup.trace"
    return run(
        [strace, "-f", "-e", "trace=execve", "-o", str(trace), *command],
        SmokeContext(cwd=consumer_root),
    )


def test_the_install_reports_the_pinned_identity(
    installed: Installed, manifest: dict[str, object]
) -> None:
    """The cold install names the pin's tag and the observed compiler."""
    assert typ.cast("str", manifest["tag"]) in installed.stdout
    assert "rustc:" in installed.stdout
    assert "kani:" in installed.stdout


def test_clean_setup_compiles_no_tool_source(
    consumer_root: Path, installed: Installed
) -> None:
    """The setup trace shows prebuilt installation and no source build."""
    trace = consumer_root / "setup.trace"
    assert trace.is_file(), f"the cold install wrote no trace:\n{installed.stderr}"
    require_prebuilt_setup(trace.read_text(encoding="utf-8"))


def test_the_effective_compiler_reaches_the_floor(installed: Installed) -> None:
    """The compiler setup selected is at or above the supported floor."""
    line = next(
        (
            candidate.removeprefix("rustc: ").strip()
            for candidate in installed.stdout.splitlines()
            if candidate.startswith("rustc: ")
        ),
        None,
    )
    assert line is not None, f"the install reported no compiler:\n{installed.stdout}"
    version = line.split(" (", 1)[0]
    require_compiler_floor(version)
    report(f"compiler {version} >= {COMPILER_FLOOR[0]}.{COMPILER_FLOOR[1]}")


def test_the_check_command_accepts_the_generation(
    consumer_root: Path, prover_tools: list[str], installed: Installed
) -> None:
    """`snapshot check` validates the installed bytes and the compiler."""
    completed = run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "check",
            "--repo-root",
            str(consumer_root),
        ],
        SmokeContext(cwd=consumer_root),
    )

    assert "checked at" in completed.stdout


def _proof(
    consumer_root: Path, prover_tools: list[str], crate: str, harness: str
) -> Installed:
    """Run one harness through the public `kani run` command."""
    return run(
        [
            *prover_tools,
            "kani",
            "run",
            "--repo-root",
            str(consumer_root),
            "--snapshot-pin",
            str(consumer_root / "tools" / "kani" / "snapshot.toml"),
            "--",
            "--harness",
            harness,
        ],
        SmokeContext(cwd=consumer_root / crate, check=False),
    )


def test_the_named_positive_harnesses_verify(
    consumer_root: Path, prover_tools: list[str], installed: Installed
) -> None:
    """Every positive harness verifies, and the report names exactly them."""
    transcript = ""
    for harness in POSITIVE_HARNESSES:
        completed = _proof(consumer_root, prover_tools, "bounded_collection", harness)
        transcript += completed.stdout + completed.stderr
        assert completed.returncode == 0, transcript[-4000:]
    report_of = require_named_harnesses(
        transcript, verified=POSITIVE_HARNESSES, failed=()
    )
    report(f"verified {sorted(report_of.verified_names)}")


def test_the_false_assertion_fails_verification(
    consumer_root: Path, prover_tools: list[str], installed: Installed
) -> None:
    """The negative fixture fails its proof, which is a non-zero exit."""
    completed = _proof(consumer_root, prover_tools, "false_assertion", FALSE_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode != 0, "a deliberately false assertion must fail"
    report_of = require_named_harnesses(
        transcript, verified=(), failed=(FALSE_HARNESS,)
    )
    report(f"failed as intended: {sorted(report_of.failed_names)}")


def test_the_floor_harness_verifies_on_the_pinned_compiler(
    consumer_root: Path, prover_tools: list[str], installed: Installed
) -> None:
    """The 1.94-only API compiles and proves under the snapshot's compiler."""
    completed = _proof(consumer_root, prover_tools, "compiler_floor", FLOOR_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode == 0, transcript[-4000:]
    require_floor_harness_verified(transcript)
    report(f"floor harness verified: {FLOOR_HARNESS}")


def test_the_below_floor_compiler_rejects_the_floor_api(tmp_path: Path) -> None:
    """The Rust 1.93 control fails because the API is unavailable.

    The control is run only when its compiler is supplied, because installing
    one is not this milestone's job. Its absence is a recorded gap, not a pass.
    """
    rustc = os.environ.get("KANI_SNAPSHOT_SMOKE_RUSTC_1_93")
    if not rustc:
        pytest.skip("KANI_SNAPSHOT_SMOKE_RUSTC_1_93 is unset")
    completed = run(
        [
            rustc,
            "--edition",
            "2021",
            "--crate-type",
            "bin",
            "-o",
            str(tmp_path / "negative-control"),
            str(FIXTURES / "compiler_floor" / "negative_1_93.rs"),
        ],
        SmokeContext(cwd=tmp_path, check=False),
    )

    assert completed.returncode != 0, "the 1.93 control must not compile"
    require_unstable_feature_rejected(completed.stderr)


def test_a_warm_repeat_reuses_the_generation(
    consumer_root: Path, prover_tools: list[str], assets: Path, installed: Installed
) -> None:
    """Repeating the install reuses the active generation without setup."""
    completed = run(
        _install_command(prover_tools, consumer_root, assets),
        SmokeContext(cwd=consumer_root),
    )

    assert "installed at" in completed.stdout


def test_a_corrupted_payload_is_refused_before_execution(
    tmp_path_factory: pytest.TempPathFactory,
    prover_tools: list[str],
    assets: Path,
    pin: SnapshotPin,
) -> None:
    """A changed payload byte stops the install, and no generation appears.

    The receiver is a fresh root with its own cache, so the healthy generation
    the earlier tests installed cannot answer for the corrupted copy.
    """
    root = tmp_path_factory.mktemp("corrupt")
    shutil.copytree(assets, root / "assets")
    backend = max(
        (root / "assets").glob("kani-*.tar.gz"), key=lambda path: path.stat().st_size
    )
    damaged = bytearray(backend.read_bytes())
    damaged[-1] ^= 0xFF
    backend.write_bytes(bytes(damaged))
    consumer = build_consumer_root(root / "consumer", pin)

    completed = run(
        _install_command(prover_tools, consumer, root / "assets"),
        SmokeContext(cwd=consumer, check=False),
    )

    output = (completed.stdout + completed.stderr).lower()
    assert completed.returncode != 0, "a corrupted payload must be refused"
    assert "checksum" in output or "digest" in output, output
    assert not list(consumer.glob(".kani-snapshots/*/generations/*")), (
        "a refused install must not publish a generation"
    )


def test_a_changed_identity_is_refused_before_setup(
    tmp_path_factory: pytest.TempPathFactory,
    prover_tools: list[str],
    assets: Path,
    pin: SnapshotPin,
) -> None:
    """A pin naming another tag cannot install this manifest."""
    root = tmp_path_factory.mktemp("identity")
    changed = SnapshotPin(
        schema_version=pin.schema_version,
        repository=pin.repository,
        tag=pin.tag.replace("-r1", "-r2"),
        upstream_commit=pin.upstream_commit,
        builder_commit=pin.builder_commit,
        target=pin.target,
        manifest_sha256=pin.manifest_sha256,
    )
    consumer = build_consumer_root(root / "consumer", changed)

    completed = run(
        _install_command(prover_tools, consumer, assets),
        SmokeContext(cwd=consumer, check=False),
    )

    assert completed.returncode != 0, "a pin naming another tag must be refused"
    assert "tag" in (completed.stdout + completed.stderr).lower()


def test_the_console_script_runs_from_a_fresh_environment(tmp_path: Path) -> None:
    """The supplied interpreter holds the package, not the checkout."""
    executable = Path(require_environment("KANI_SNAPSHOT_SMOKE_PROVER_TOOLS")).resolve()
    environment = executable.parent.parent
    assert (environment / "pyvenv.cfg").is_file(), (
        "the supplied prover-tools is not a virtual environment"
    )
    completed = run(
        [str(executable), "--help"], SmokeContext(cwd=tmp_path, timeout=120)
    )

    assert "kani" in completed.stdout
