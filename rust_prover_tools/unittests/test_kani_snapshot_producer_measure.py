"""The build job's evidence is measured, not asserted.

The workflow cannot run in the unit suite, so the build job's only job here is
to hand `measure_build` a sysroot and trust what comes back. These tests build a
throwaway sysroot whose `rustc` and `kani-compiler` are scripts, then pin three
properties: every recorded digest is the digest of the measured bytes, every
probe is a real command rather than a version string, and the recorded library
set is exactly the set the consumer's own walk requires. A missing or failing
input is a refusal.
"""

from __future__ import annotations

import pathlib

import pytest

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.orchestrator.build_evidence import (
    parse_build_evidence,
)
from rust_prover_tools.kani_snapshot.orchestrator.measure_build import (
    build_parser,
    main,
    measure,
)
from rust_prover_tools.kani_snapshot.shared_libraries import shared_libraries
from rust_prover_tools.kani_snapshot.toolchain_model import components_digest

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
        "--lock",
        str(lockfile),
        "--submodule",
        "library=0d2328a93f0e0ff66132d6bfa1a7d884877cf862",
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
    assert toolchain.rustup_components_sha256 == components_digest(
        (sysroot / "lib" / "rustlib" / "components").read_bytes()
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


def test_a_toolchain_without_a_shared_library_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A toolchain with nothing to record cannot be described by the manifest."""
    sysroot = _sysroot(tmp_path)
    (sysroot / LIBRARY).unlink()
    arguments = _arguments(tmp_path, sysroot)

    assert main(arguments) == 1
    assert not (tmp_path / "evidence.json").exists()


def test_the_recorded_libraries_are_the_set_the_consumer_checks(
    tmp_path: pathlib.Path,
) -> None:
    """Producer and consumer select the same files, including extra ones.

    The consumer refuses to install a toolchain carrying a shared object the
    manifest does not record, so recording only `libstd` would publish a
    snapshot no consumer accepts. The measurement therefore enumerates the
    toolchain rather than trusting the job to name the files.
    """
    sysroot = _sysroot(tmp_path)
    extra = sysroot / "lib" / "libextra.so"
    extra.write_bytes(b"\x7fELF extra shared object")
    versioned = sysroot / "lib" / "rustlib" / "x86_64-unknown-linux-gnu" / "lib"
    versioned.mkdir(parents=True, exist_ok=True)
    (versioned / "libLLVM-21.so.1").write_bytes(b"\x7fELF LLVM shared object")

    measured = measure(build_parser().parse_args(_arguments(tmp_path, sysroot)))

    assert [record.path for record in measured.toolchain.libraries] == list(
        shared_libraries(sysroot)
    )
    assert set(shared_libraries(sysroot)) == {
        LIBRARY,
        "lib/libextra.so",
        "lib/rustlib/x86_64-unknown-linux-gnu/lib/libLLVM-21.so.1",
    }


def _aliased_sysroot(base: pathlib.Path, *, link_first: bool) -> pathlib.Path:
    """Create a sysroot whose shared object is reachable under two names."""
    (base / TOOLCHAIN / "lib").mkdir(parents=True)
    alias = base / TOOLCHAIN / "lib" / "aaa-alias"
    if link_first:
        # A dangling link now, filled in by `_sysroot`, so the alias is the
        # first entry this directory ever holds.
        alias.symlink_to("rustlib")
    sysroot = _sysroot(base)
    if not link_first:
        alias.symlink_to("rustlib")
    return sysroot


def test_the_library_set_does_not_depend_on_directory_order(
    tmp_path: pathlib.Path,
) -> None:
    """One toolchain yields one set of paths however its aliases appeared.

    `os.scandir` reports entries in whatever order the filesystem holds them,
    which differs between filesystems and machines. A walk that recorded the
    name it met a file under would spell the same library as either
    `lib/rustlib/...` or `lib/aaa-alias/...` depending on that order, so the
    manifest would not describe the same bytes as the consumer's own walk.
    Both copies below hold identical trees built in opposite orders.
    """
    forward = _aliased_sysroot(tmp_path / "forward", link_first=False)
    backward = _aliased_sysroot(tmp_path / "backward", link_first=True)

    assert shared_libraries(forward) == shared_libraries(backward) == (LIBRARY,)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    recorded = [
        [
            record.path
            for record in measure(
                build_parser().parse_args(_arguments(scratch, sysroot))
            ).toolchain.libraries
        ]
        for sysroot in (forward, backward)
    ]
    assert recorded == [list(shared_libraries(forward))] * 2


def test_a_library_alias_is_recorded_and_bound_to_its_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """A file alias is part of the toolchain identity, not an invisible spelling.

    Recording only the canonical file would let an alias be retargeted or
    deleted without changing the manifest, so an altered library-loading
    configuration could pass admission. The alias name is recorded, which means
    retargeting it changes the digest recorded at that path and deleting it
    removes a recorded name.
    """
    sysroot = _sysroot(tmp_path)
    library = pathlib.PurePosixPath(LIBRARY)
    alias = sysroot / "lib" / "libstd-alias.so"
    alias.symlink_to(library.relative_to("lib"))

    recorded = shared_libraries(sysroot)
    assert "lib/libstd-alias.so" in recorded, recorded
    assert LIBRARY in recorded, recorded
    before = sha256_file(sysroot / "lib" / "libstd-alias.so")

    replacement = sysroot / library.parent / "libstd-def456.so"
    replacement.write_bytes(b"\x7fELF a different standard library")
    alias.unlink()
    alias.symlink_to(library.relative_to("lib").parent / replacement.name)

    assert "lib/libstd-alias.so" in shared_libraries(sysroot), (
        "the alias name is still recorded after it is retargeted"
    )
    assert sha256_file(alias) != before, "the recorded path now hashes other bytes"

    alias.unlink()
    assert "lib/libstd-alias.so" not in shared_libraries(sysroot), (
        "a deleted alias is no longer part of the toolchain identity"
    )


def test_a_library_link_out_of_the_toolchain_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A shared object reached outside the sysroot is not the measured tree."""
    sysroot = _sysroot(tmp_path)
    outside = tmp_path / "outside.so"
    outside.write_bytes(b"\x7fELF outside shared object")
    (sysroot / "lib" / "liboutside.so").symlink_to(outside)

    with pytest.raises(ProverToolError, match="escapes its toolchain"):
        measure(build_parser().parse_args(_arguments(tmp_path, sysroot)))


def test_a_failing_compiler_probe_is_refused(tmp_path: pathlib.Path) -> None:
    """A probe that could not run leaves the identity unknown, not blank."""
    sysroot = _sysroot(tmp_path)
    _script(sysroot / "bin" / "rustc", "echo 'broken toolchain' >&2\nexit 2")
    arguments = _arguments(tmp_path, sysroot)

    assert main(arguments) == 1
    assert not (tmp_path / "evidence.json").exists()


def test_a_malformed_solver_measurement_is_refused(tmp_path: pathlib.Path) -> None:
    """The `name=version=sha256` form is what the manifest records."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    solver = arguments.index("--solver")
    arguments[solver + 1] = "kissat=4.0.1"

    assert main(arguments) == 1


@pytest.mark.parametrize("value", ["-1", "-0.5"])
def test_a_negative_wall_measurement_is_refused(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A negative duration is evidence the manifest parser would reject."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    arguments[arguments.index("--wall-seconds") + 1] = value

    assert main(arguments) == 1
    assert "finite and non-negative" in capsys.readouterr().err
    assert not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400"])
def test_a_non_finite_wall_measurement_is_refused(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """Infinity and NaN are not measurements the evidence document can hold."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    # `--wall-seconds -inf` would be read as an option, so keep the value in
    # the `--flag=value` form that accepts a leading minus.
    index = arguments.index("--wall-seconds")
    arguments[index : index + 2] = [f"--wall-seconds={value}"]

    assert main(arguments) == 1
    assert "finite and non-negative" in capsys.readouterr().err
    assert not (tmp_path / "evidence.json").exists()


def test_a_zero_wall_measurement_is_valid(tmp_path: pathlib.Path) -> None:
    """Zero is finite and non-negative, so it remains a valid measurement."""
    arguments = _arguments(tmp_path, _sysroot(tmp_path))
    index = arguments.index("--wall-seconds")
    arguments[index : index + 2] = ["--wall-seconds=0"]

    assert main(arguments) == 0

    measured = parse_build_evidence((tmp_path / "evidence.json").read_bytes())
    assert measured.wall_seconds == pytest.approx(0.0)


def test_an_unreadable_components_file_is_reported(tmp_path: pathlib.Path) -> None:
    """A missing components file fails with a clear error, not a bare OSError."""
    sysroot = _sysroot(tmp_path)
    (sysroot / "lib" / "rustlib" / "components").unlink()

    with pytest.raises(ProverToolError, match="rustup components list"):
        measure(build_parser().parse_args(_arguments(tmp_path, sysroot)))


def test_the_components_digest_is_over_the_canonical_names(
    tmp_path: pathlib.Path,
) -> None:
    """The measurement canonicalizes; a raw-bytes digest would fail here.

    The record is unsorted and has no trailing newline, so its own SHA-256
    differs from the digest of the sorted names. Only a measurement that reads
    through `canonical_component_names` can produce the expected value.
    """
    sysroot = _sysroot(tmp_path)
    record = sysroot / "lib" / "rustlib" / "components"
    record.write_bytes(b"rustc\ncargo\nrust-std")

    measured = measure(build_parser().parse_args(_arguments(tmp_path, sysroot)))
    toolchain = measured.toolchain

    assert toolchain.rustup_components_sha256 == components_digest(
        b"cargo\nrust-std\nrustc\n"
    )
    assert toolchain.rustup_components_sha256 != sha256_file(record), (
        "a raw-byte digest of the record must not satisfy the canonical value"
    )


def test_a_duplicated_component_is_refused_by_the_measurement(
    tmp_path: pathlib.Path,
) -> None:
    """A record listing one component twice is ambiguous, so measuring fails."""
    sysroot = _sysroot(tmp_path)
    record = sysroot / "lib" / "rustlib" / "components"
    record.write_bytes(b"rustc\ncargo\nrustc\n")

    with pytest.raises(ProverToolError, match="more than once"):
        measure(build_parser().parse_args(_arguments(tmp_path, sysroot)))
