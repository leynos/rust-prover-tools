"""The build job's evidence is measured, not asserted.

The workflow cannot run in the unit suite, so the build job's only job here is
to hand `measure_build` a sysroot and a set of paths and trust what comes back.
These tests build a throwaway sysroot whose `rustc` and `kani-compiler` are
scripts, then pin three properties: every recorded digest is the digest of the
named bytes, every probe is a real command rather than a version string, and a
missing or failing input is a refusal.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.build_evidence import (
    parse_build_evidence,
)
from rust_prover_tools.kani_snapshot.producer.measure_build import (
    build_parser,
    main,
    measure,
)

if typ.TYPE_CHECKING:
    import pathlib

RUSTC_VV = """\
rustc 1.95.0-nightly (0d2328a93 2026-08-21)
binary: rustc
commit-hash: 0d2328a93f0e0ff66132d6bfa1a7d884877cf862
commit-date: 2026-08-21
host: x86_64-unknown-linux-gnu
release: 1.95.0-nightly
LLVM version: 21.1.0
"""

TOOLCHAIN = "nightly-2026-08-21-x86_64-unknown-linux-gnu"
LIBRARY = "lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-abc123.so"


def _script(path: pathlib.Path, body: str) -> pathlib.Path:
    """Write an executable stand-in for a compiler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _sysroot(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a sysroot shaped like a rustup toolchain directory."""
    sysroot = tmp_path / TOOLCHAIN
    _script(sysroot / "bin" / "rustc", f"cat <<'EOF'\n{RUSTC_VV}EOF")
    (sysroot / "lib" / "rustlib").mkdir(parents=True)
    (sysroot / "lib" / "rustlib" / "components").write_text(
        "rustc-x86_64-unknown-linux-gnu\n"
    )
    library = sysroot / LIBRARY
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\x7fELF pretend standard library")
    return sysroot


def _arguments(tmp_path: pathlib.Path, sysroot: pathlib.Path) -> list[str]:
    """Return a complete, valid measurement command line."""
    channel = tmp_path / "channel-rust-nightly.toml"
    channel.write_text("manifest-version = '2'\n")
    compiler = _script(
        tmp_path / "bundle" / "bin" / "kani-compiler",
        "echo 'kani-compiler 0.68.0 (rustc 1.95.0-nightly)'",
    )
    lockfile = tmp_path / "Cargo.lock"
    lockfile.write_text("# lock\n")
    solver = tmp_path / "bundle" / "bin" / "kissat"
    solver.parent.mkdir(parents=True, exist_ok=True)
    solver.write_bytes(b"kissat binary")
    return [
        "--sysroot",
        str(sysroot),
        "--channel-manifest",
        str(channel),
        "--kani-compiler",
        str(compiler),
        "--backend-version",
        "0.68.0",
        "--upstream-commit",
        "0d2328a93f0e0ff66132d6bfa1a7d884877cf862",
        "--launcher-package-version",
        "0.68.0",
        "--library",
        LIBRARY,
        "--lock",
        str(lockfile),
        "--solver",
        f"kissat=4.0.1={sha256_file(solver)}",
        "--cache-outcome",
        "miss",
        "--cache-key",
        "sccache-v1",
        "--wall-seconds",
        "812.5",
        "--disk-high-water-bytes",
        "7340032000",
        "--output",
        str(tmp_path / "evidence.json"),
    ]


def test_the_measurement_records_what_it_observed(tmp_path: pathlib.Path) -> None:
    """Every digest is over the named bytes, and probes are real runs."""
    sysroot = _sysroot(tmp_path)
    arguments = _arguments(tmp_path, sysroot)

    assert main(arguments) == 0

    measured = parse_build_evidence((tmp_path / "evidence.json").read_bytes())
    toolchain = measured.toolchain
    assert toolchain.identifier == TOOLCHAIN
    assert toolchain.rustc_vv == RUSTC_VV.strip()
    assert toolchain.kani_compiler == "kani-compiler 0.68.0 (rustc 1.95.0-nightly)"
    assert toolchain.rustc_sha256 == sha256_file(sysroot / "bin" / "rustc")
    assert toolchain.rustup_components_sha256 == sha256_file(
        sysroot / "lib" / "rustlib" / "components"
    )
    assert toolchain.channel_metadata_sha256 == sha256_file(
        tmp_path / "channel-rust-nightly.toml"
    )
    assert toolchain.components == ()
    assert [(record.path, record.sha256) for record in toolchain.libraries] == [
        (LIBRARY, sha256_file(sysroot / LIBRARY))
    ]
    assert measured.backend.revision == "0d2328a93f0e0ff66132d6bfa1a7d884877cf862"
    assert measured.backend.solvers[0].name == "kissat"
    assert measured.backend.solvers[0].version == "4.0.1"
    assert measured.lock_digests == (
        (str(tmp_path / "Cargo.lock"), sha256_file(tmp_path / "Cargo.lock")),
    )
    assert measured.cache.outcome == "miss"
    assert measured.wall_seconds == pytest.approx(812.5)
    assert measured.disk_high_water_bytes == 7340032000


def test_a_toolchain_without_a_recorded_library_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """An unrecorded library would be distributed with no digest at all."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    library = arguments.index("--library")
    del arguments[library : library + 2]

    assert main(arguments) == 1
    assert not (tmp_path / "evidence.json").exists()


def test_a_failing_compiler_probe_is_refused(tmp_path: pathlib.Path) -> None:
    """A probe that could not run leaves the identity unknown, not blank."""
    sysroot = _sysroot(tmp_path)
    _script(sysroot / "bin" / "rustc", "echo 'broken toolchain' >&2\nexit 2")
    arguments = _arguments(tmp_path, sysroot)

    assert main(arguments) == 1
    assert not (tmp_path / "evidence.json").exists()


def test_a_missing_library_file_is_refused(tmp_path: pathlib.Path) -> None:
    """A path the job named but the toolchain lacks is a failed measurement."""
    sysroot = _sysroot(tmp_path)
    (sysroot / LIBRARY).unlink()
    arguments = _arguments(tmp_path, sysroot)

    with pytest.raises(ProverToolError, match="not a regular file"):
        measure(build_parser().parse_args(arguments))


def test_a_malformed_solver_measurement_is_refused(tmp_path: pathlib.Path) -> None:
    """The `name=version=sha256` form is what the manifest records."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    solver = arguments.index("--solver")
    arguments[solver + 1] = "kissat=4.0.1"

    assert main(arguments) == 1
