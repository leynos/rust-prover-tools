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
"""

from __future__ import annotations

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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("KANI_SNAPSHOT_SMOKE"),
        reason="set KANI_SNAPSHOT_SMOKE=1 through make kani-snapshot-verify",
    ),
    pytest.mark.timeout(1800),
]

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

REPO_ROOT: typ.Final[Path] = Path(__file__).resolve().parents[2]
FIXTURES: typ.Final[Path] = REPO_ROOT / "tests" / "fixtures" / "kani_snapshot"


def _required(name: str) -> str:
    """Return one required environment variable or fail with its name."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must name an existing path")
    return value


def _asset_dir() -> Path:
    """Return the assembled release directory."""
    return Path(_required("KANI_SNAPSHOT_SMOKE_ASSET_DIR")).resolve()


def _report(path: Path) -> None:
    """Print a bounded evidence note, so a passing run is legible in the log."""
    print(f"[kani-smoke] {path}")


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: cabc.Mapping[str, str] | None = None,
    timeout: int = PROOF_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one public command with combined capture."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        pytest.fail(
            f"{' '.join(command)} exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    return completed


@pytest.fixture(scope="module")
def assets() -> Path:
    """Return the release directory and require its complete file set."""
    directory = _asset_dir()
    required = {
        "manifest.json",
        "manifest.sigstore.jsonl",
        "SHA256SUMS",
        "inventory.json",
    }
    missing = sorted(name for name in required if not (directory / name).is_file())
    assert not missing, f"the release directory lacks {missing}"
    return directory


@pytest.fixture(scope="module")
def manifest(assets: Path) -> dict[str, object]:
    """Return the produced manifest, refusing a document of the wrong shape."""
    document = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the manifest must be a JSON object"
    return typ.cast("dict[str, object]", document)


@pytest.fixture(scope="module")
def pin(assets: Path, manifest: dict[str, object]) -> SnapshotPin:
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


@pytest.fixture(scope="module")
def prover_tools() -> list[str]:
    """Return the command vector that runs the installed console script."""
    executable = _required("KANI_SNAPSHOT_SMOKE_PROVER_TOOLS")
    return [executable]


@pytest.fixture(scope="module")
def consumer_root(tmp_path_factory: pytest.TempPathFactory, pin: SnapshotPin) -> Path:
    """Build a consumer repository holding the fixtures and the reviewed pin."""
    root = tmp_path_factory.mktemp("consumer")
    for crate in ("bounded_collection", "false_assertion", "compiler_floor"):
        shutil.copytree(FIXTURES / "proofs" / crate, root / crate)
    (root / "tools" / "kani").mkdir(parents=True)
    (root / "tools" / "kani" / "snapshot.toml").write_bytes(pin_to_toml(pin))
    _report(root)
    return root


@pytest.fixture(scope="module")
def installed(
    consumer_root: Path, prover_tools: list[str], assets: Path
) -> subprocess.CompletedProcess[str]:
    """Install the produced snapshot through the public command, cold.

    Setup is traced when `strace` is available, so the descendant-process
    policy is discharged from the same run rather than a separate one.
    """
    strace = shutil.which("strace")
    command = [
        *prover_tools,
        "kani",
        "snapshot",
        "install",
        "--repo-root",
        str(consumer_root),
        "--asset-dir",
        str(assets),
    ]
    if strace is None:
        return _run(command, cwd=consumer_root)
    trace = consumer_root / "setup.trace"
    return _run(
        [strace, "-f", "-e", "trace=execve", "-o", str(trace), *command],
        cwd=consumer_root,
    )


def test_the_install_reports_the_pinned_identity(
    installed: subprocess.CompletedProcess[str], manifest: dict[str, object]
) -> None:
    """The cold install names the pin's tag and the observed compiler."""
    assert typ.cast("str", manifest["tag"]) in installed.stdout
    assert "rustc:" in installed.stdout
    assert "kani:" in installed.stdout


def test_clean_setup_compiles_no_tool_source(
    consumer_root: Path, installed: subprocess.CompletedProcess[str]
) -> None:
    """The setup trace shows prebuilt installation and no source build.

    The trace is written by the cold install when `strace` exists. A host
    without it records the gap rather than claiming the check ran.
    """
    trace = consumer_root / "setup.trace"
    if not trace.is_file():
        pytest.skip("strace is unavailable, so descendant execution is unobserved")
    require_prebuilt_setup(trace.read_text(encoding="utf-8"))


def test_the_effective_compiler_reaches_the_floor(
    installed: subprocess.CompletedProcess[str],
) -> None:
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
    _report(f"compiler {version} >= {COMPILER_FLOOR[0]}.{COMPILER_FLOOR[1]}")


def test_the_check_command_accepts_the_generation(
    consumer_root: Path,
    prover_tools: list[str],
    installed: subprocess.CompletedProcess[str],
) -> None:
    """`snapshot check` validates the installed bytes and the compiler."""
    completed = _run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "check",
            "--repo-root",
            str(consumer_root),
        ],
        cwd=consumer_root,
    )

    assert "checked at" in completed.stdout


def _proof(
    consumer_root: Path, prover_tools: list[str], crate: str, harness: str
) -> subprocess.CompletedProcess[str]:
    """Run one harness through the public `kani run` command."""
    return _run(
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
        cwd=consumer_root / crate,
        check=False,
    )


def test_the_named_positive_harnesses_verify(
    consumer_root: Path,
    prover_tools: list[str],
    installed: subprocess.CompletedProcess[str],
) -> None:
    """Every positive harness verifies, and the report names exactly them."""
    transcript = ""
    for harness in POSITIVE_HARNESSES:
        completed = _proof(consumer_root, prover_tools, "bounded_collection", harness)
        transcript += completed.stdout + completed.stderr
        assert completed.returncode == 0, transcript[-4000:]
    report = require_named_harnesses(transcript, verified=POSITIVE_HARNESSES, failed=())
    _report(f"verified {sorted(report.verified_names)}")


def test_the_false_assertion_fails_verification(
    consumer_root: Path,
    prover_tools: list[str],
    installed: subprocess.CompletedProcess[str],
) -> None:
    """The negative fixture fails its proof, which is a non-zero exit."""
    completed = _proof(consumer_root, prover_tools, "false_assertion", FALSE_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode != 0, "a deliberately false assertion must fail"
    report = require_named_harnesses(transcript, verified=(), failed=(FALSE_HARNESS,))
    _report(f"failed as intended: {sorted(report.failed_names)}")


def test_the_floor_harness_verifies_on_the_pinned_compiler(
    consumer_root: Path,
    prover_tools: list[str],
    installed: subprocess.CompletedProcess[str],
) -> None:
    """The 1.94-only API compiles and proves under the snapshot's compiler."""
    completed = _proof(consumer_root, prover_tools, "compiler_floor", FLOOR_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode == 0, transcript[-4000:]
    require_floor_harness_verified(transcript)
    _report(f"floor harness verified: {FLOOR_HARNESS}")


def test_the_below_floor_compiler_rejects_the_floor_api(
    tmp_path: Path,
) -> None:
    """The Rust 1.93 control fails because the API is unavailable.

    The control is run only when its compiler is supplied, because installing
    one is not this milestone's job. Its absence is a recorded gap, not a pass.
    """
    rustc = os.environ.get("KANI_SNAPSHOT_SMOKE_RUSTC_1_93")
    if not rustc:
        pytest.skip("KANI_SNAPSHOT_SMOKE_RUSTC_1_93 is unset")
    completed = _run(
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
        cwd=tmp_path,
        check=False,
    )

    assert completed.returncode != 0, "the 1.93 control must not compile"
    require_unstable_feature_rejected(completed.stderr)


def test_a_warm_repeat_reuses_the_generation(
    consumer_root: Path,
    prover_tools: list[str],
    assets: Path,
    installed: subprocess.CompletedProcess[str],
) -> None:
    """Repeating the install reuses the active generation without setup."""
    completed = _run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "install",
            "--repo-root",
            str(consumer_root),
            "--asset-dir",
            str(assets),
        ],
        cwd=consumer_root,
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
    shutil.copytree(FIXTURES / "proofs" / "bounded_collection", root / "consumer")
    (root / "consumer" / "tools" / "kani").mkdir(parents=True)
    (root / "consumer" / "tools" / "kani" / "snapshot.toml").write_bytes(
        pin_to_toml(pin)
    )

    completed = _run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "install",
            "--repo-root",
            str(root / "consumer"),
            "--asset-dir",
            str(root / "assets"),
        ],
        cwd=root / "consumer",
        check=False,
    )

    assert completed.returncode != 0, "a corrupted payload must be refused"
    assert "checksum" in (completed.stdout + completed.stderr).lower() or (
        "digest" in (completed.stdout + completed.stderr).lower()
    ), completed.stdout + completed.stderr
    assert not (root / "consumer" / ".kani-snapshots").exists() or not any(
        path.name.startswith("generation-")
        for path in (root / "consumer" / ".kani-snapshots").rglob("generation-*")
    ), "a refused install must not publish a generation"


def test_a_changed_identity_is_refused_before_setup(
    tmp_path_factory: pytest.TempPathFactory,
    prover_tools: list[str],
    assets: Path,
    pin: SnapshotPin,
) -> None:
    """A pin naming another tag cannot install this manifest."""
    root = tmp_path_factory.mktemp("identity")
    shutil.copytree(FIXTURES / "proofs" / "bounded_collection", root / "consumer")
    (root / "consumer" / "tools" / "kani").mkdir(parents=True)
    changed = SnapshotPin(
        schema_version=pin.schema_version,
        repository=pin.repository,
        tag=pin.tag.replace("-r1", "-r2"),
        upstream_commit=pin.upstream_commit,
        builder_commit=pin.builder_commit,
        target=pin.target,
        manifest_sha256=pin.manifest_sha256,
    )
    (root / "consumer" / "tools" / "kani" / "snapshot.toml").write_bytes(
        pin_to_toml(changed)
    )

    completed = _run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "install",
            "--repo-root",
            str(root / "consumer"),
            "--asset-dir",
            str(assets),
        ],
        cwd=root / "consumer",
        check=False,
    )

    assert completed.returncode != 0, "a pin naming another tag must be refused"
    assert "tag" in (completed.stdout + completed.stderr).lower()


def test_the_console_script_runs_from_a_fresh_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The supplied interpreter holds the package, not the checkout.

    A wheel installed into a fresh virtual environment is the consumer's real
    delivery path; importing from this checkout would hide a packaging defect.
    """
    executable = Path(_required("KANI_SNAPSHOT_SMOKE_PROVER_TOOLS")).resolve()
    environment = executable.parent.parent
    if not (environment / "pyvenv.cfg").is_file():
        pytest.skip("the supplied prover-tools is not a virtual environment")
    completed = _run(
        [str(executable), "--help"], cwd=tmp_path_factory.mktemp("help"), timeout=120
    )

    assert "kani" in completed.stdout
