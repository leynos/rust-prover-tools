"""Opt-in end-to-end verification of one produced Kani snapshot.

This module is the consumer half of EP-M5. It takes a release directory that a
non-publishing hosted run assembled, installs it through the public commands in
a fresh environment, and requires the evidence D6 and V8 name: the effective
compiler at or above the floor, the exact named positive harnesses, a
deliberately false assertion that failed verification rather than compilation,
a compiler-floor harness that only Rust 1.94 accepts, the Rust 1.93 control,
a warm-cache repeat, and a corrupted payload and changed identity refused
before execution.

The public `kani run` command executes the standalone `kani` launcher from the
repository root, so each proof root holds the fixture source file the launcher
compiles. Fixtures live in `tests/integration/conftest.py`; the remaining
plumbing lives in `tests/kani_snapshot_smoke_support.py`.

It is skipped unless `KANI_SNAPSHOT_SMOKE` is set. `make kani-snapshot-verify`
supplies the environment; ordinary `make test` never reaches it.
"""

from __future__ import annotations

import dataclasses as dc
import os
import shutil
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_snapshot.producer.compiler_floor import (
    COMPILER_FLOOR,
    parse_rustc_version,
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
from tests.kani_snapshot_smoke_commands import (
    install_command,
    proof,
    require_no_setup,
    run_command,
    traced_run,
)
from tests.kani_snapshot_smoke_support import (
    FALSE_HARNESS,
    FIXTURES,
    FLOOR_HARNESS,
    POSITIVE_HARNESSES,
    UPSTREAM_BUNDLE_HARNESSES,
    Consumer,
    SmokeContext,
    build_root,
    generation_from_output,
    read_trace,
    report,
    require_environment,
    run,
    snapshot_environment,
)

if typ.TYPE_CHECKING:
    import subprocess

    from rust_prover_tools.kani_snapshot.models import SnapshotPin

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("KANI_SNAPSHOT_SMOKE"),
        reason="set KANI_SNAPSHOT_SMOKE=1 through make kani-snapshot-verify",
    ),
    pytest.mark.timeout(1800),
]

type Completed = subprocess.CompletedProcess[str]


def test_the_install_reports_the_pinned_identity(
    installed: Completed, manifest: dict[str, object]
) -> None:
    """The cold install names the pin's tag and the observed compiler."""
    assert typ.cast("str", manifest["tag"]) in installed.stdout
    assert "rustc:" in installed.stdout
    assert "kani:" in installed.stdout


def test_clean_setup_compiles_no_tool_source(
    consumer: Consumer, installed: Completed
) -> None:
    """The setup trace shows prebuilt installation and no source build."""
    trace = consumer.roots["bounded_collection"] / "setup.trace"
    assert trace.is_file(), "the cold install wrote no trace; strace is required"
    require_prebuilt_setup(read_trace(trace))


def test_the_effective_compiler_reaches_the_floor(installed: Completed) -> None:
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
    consumer: Consumer, prover_tools: list[str], installed: Completed
) -> None:
    """`snapshot check` validates the installed bytes and the compiler."""
    root = consumer.roots["bounded_collection"]
    completed = run(
        [
            *prover_tools,
            "kani",
            "snapshot",
            "check",
            "--repo-root",
            str(root),
            "--cache-dir",
            str(consumer.cache),
        ],
        SmokeContext(cwd=root),
    )

    assert "checked at" in completed.stdout


def test_the_upstream_launcher_reports_the_pinned_identity(
    consumer: Consumer, prover_tools: list[str], manifest: dict[str, object]
) -> None:
    """The bundle's own launcher reports the version the manifest records.

    This is the upstream bundle smoke: the pinned launcher executes and names
    the version the produced manifest declares, before any proof runs.
    """
    root = consumer.roots["bounded_collection"]
    backend = typ.cast("dict[str, object]", manifest["backend"])
    completed = run(
        run_command(prover_tools, root, consumer.cache, ("--version",)),
        SmokeContext(cwd=root),
    )

    assert typ.cast("str", backend["version"]) in completed.stdout


def test_the_named_positive_harnesses_verify(
    consumer: Consumer, prover_tools: list[str], installed: Completed
) -> None:
    """Each positive harness verifies in its own run and transcript."""
    for harness in POSITIVE_HARNESSES:
        completed = proof(consumer, prover_tools, "bounded_collection", harness)
        transcript = completed.stdout + completed.stderr
        assert completed.returncode == 0, transcript[-4000:]
        require_named_harnesses(transcript, verified=(harness,), failed=())
        assert "unwinding assertion" not in transcript.lower(), (
            f"{harness} reported an unwinding assertion failure"
        )
        report(f"verified {harness}")


def test_the_false_assertion_fails_verification(
    consumer: Consumer, prover_tools: list[str], installed: Completed
) -> None:
    """The negative fixture fails its proof for the intended reason."""
    completed = proof(consumer, prover_tools, "false_assertion", FALSE_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode != 0, "a deliberately false assertion must fail"
    assert "Failed Checks: deliberately false" in transcript, transcript[-4000:]
    report_of = require_named_harnesses(
        transcript, verified=(), failed=(FALSE_HARNESS,)
    )
    report(f"failed as intended: {sorted(report_of.failed_names)}")


def test_the_floor_harness_verifies_on_the_pinned_compiler(
    consumer: Consumer, prover_tools: list[str], installed: Completed
) -> None:
    """The 1.94-only API compiles and proves under the snapshot's compiler."""
    completed = proof(consumer, prover_tools, "compiler_floor", FLOOR_HARNESS)
    transcript = completed.stdout + completed.stderr

    assert completed.returncode == 0, transcript[-4000:]
    require_named_harnesses(transcript, verified=(FLOOR_HARNESS,), failed=())
    require_floor_harness_verified(transcript)
    assert "unwinding assertion" not in transcript.lower(), (
        "the floor harness reported an unwinding assertion failure"
    )
    report(f"floor harness verified: {FLOOR_HARNESS}")


def test_the_below_floor_compiler_rejects_the_floor_api(tmp_path: Path) -> None:
    """The required Rust 1.93 control fails because the API is unavailable."""
    rustc = os.environ.get("KANI_SNAPSHOT_SMOKE_RUSTC_1_93")
    assert rustc, "KANI_SNAPSHOT_SMOKE_RUSTC_1_93 must name the 1.93 compiler"
    version = run(
        [rustc, "--version"], SmokeContext(cwd=tmp_path, timeout=120)
    ).stdout.strip()
    assert parse_rustc_version(version) == (1, 93), (
        f"the control compiler {version!r} is not the required 1.93 release"
    )
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


def _snapshot_cargo(consumer: Consumer, crate: str, trace: Path) -> Completed:
    """Run `cargo kani` in one crate with the snapshot's own environment.

    Upstream's bundle test runs `cargo kani` for the `simple-lib`,
    `build-rs-works` and `simple-kissat` fixtures. The generation's `PATH`
    selects the pinned launcher and toolchain, and its homes are private, so
    this is the same route a consumer uses. The run is traced so a missing
    generation or an unintended setup shows up as evidence rather than silence.
    """
    root = consumer.roots[crate]
    environment = snapshot_environment(consumer.runtime(root))
    return traced_run(
        ["cargo", "kani"],
        trace,
        SmokeContext(cwd=root, env=environment, replace_env=True),
    )


def test_the_upstream_bundle_fixtures_verify(
    consumer: Consumer, installed: Completed
) -> None:
    """Upstream's own TestBundle scenarios run through the snapshot.

    Upstream's release workflow runs `cargo kani` in `simple-lib`,
    `build-rs-works` and `simple-kissat`. This repository carries those three
    crates, so running `cargo kani` in each under the generation's own
    environment reproduces the same scenarios, including the manifest that
    selects Kissat through `--external-sat-solver`. Their manifests carry a
    `[workspace]` entry so each stands alone.
    """
    for crate, harnesses in UPSTREAM_BUNDLE_HARNESSES.items():
        trace = consumer.roots[crate] / "cargo-kani.trace"
        completed = _snapshot_cargo(consumer, crate, trace)
        transcript = completed.stdout + completed.stderr
        assert completed.returncode == 0, f"{crate}: {transcript[-4000:]}"
        # Each expectation is the exact crate-relative name the pinned driver
        # prints, so a harness from another module cannot satisfy it.
        require_named_harnesses(transcript, verified=harnesses, failed=())
        report(f"upstream bundle smoke verified {crate}: {', '.join(harnesses)}")


def test_a_warm_repeat_reuses_the_generation(
    consumer: Consumer,
    prover_tools: list[str],
    assets: Path,
    installed: Completed,
) -> None:
    """Repeating the install reuses the generation and runs no setup."""
    root = consumer.roots["bounded_collection"]
    trace = root / "warm.trace"
    completed = traced_run(
        install_command(prover_tools, root, assets, consumer.cache),
        trace,
        SmokeContext(cwd=root),
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    first = generation_from_output(installed.stdout)
    repeat = generation_from_output(completed.stdout)
    assert first is not None, "the cold install named no generation"
    assert repeat == first, (
        f"the warm repeat did not reuse the generation: {first!r} vs {repeat!r}"
    )
    require_no_setup(trace)


def test_a_corrupted_payload_is_refused_before_execution(
    tmp_path_factory: pytest.TempPathFactory,
    prover_tools: list[str],
    assets: Path,
    pin: SnapshotPin,
) -> None:
    """A changed payload byte stops the install before setup, with no generation."""
    root = tmp_path_factory.mktemp("corrupt")
    shutil.copytree(assets, root / "assets")
    backend = max(
        (root / "assets").glob("kani-*.tar.gz"), key=lambda path: path.stat().st_size
    )
    damaged = bytearray(backend.read_bytes())
    damaged[-1] ^= 0xFF
    backend.write_bytes(bytes(damaged))
    consumer_root = build_root(root / "consumer", pin, "bounded_collection")
    trace = root / "corrupt.trace"

    completed = traced_run(
        install_command(prover_tools, consumer_root, root / "assets", root / "cache"),
        trace,
        SmokeContext(cwd=consumer_root),
    )

    output = (completed.stdout + completed.stderr).lower()
    assert completed.returncode != 0, "a corrupted payload must be refused"
    assert "checksum" in output or "digest" in output, output
    require_no_setup(trace)
    assert not list((root / "cache").glob("*/generations/*")), (
        "a refused install must not publish a generation"
    )


def test_a_changed_identity_is_refused_before_setup(
    tmp_path_factory: pytest.TempPathFactory,
    prover_tools: list[str],
    assets: Path,
    pin: SnapshotPin,
) -> None:
    """A pin naming another revision cannot install this manifest."""
    stem, _, revision = pin.tag.rpartition("-r")
    changed = dc.replace(pin, tag=f"{stem}-r{int(revision) + 1}")
    assert changed.tag != pin.tag, "the changed identity must differ from the pin"
    root = tmp_path_factory.mktemp("identity")
    consumer_root = build_root(root / "consumer", changed, "bounded_collection")
    trace = root / "identity.trace"

    completed = traced_run(
        install_command(prover_tools, consumer_root, assets, root / "cache"),
        trace,
        SmokeContext(cwd=consumer_root),
    )

    assert completed.returncode != 0, "a pin naming another revision must be refused"
    assert "tag" in (completed.stdout + completed.stderr).lower()
    require_no_setup(trace)


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
