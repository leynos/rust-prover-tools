"""Opt-in evidence that the selected release shadows a development backend.

These cases exercise the real Cargo shipped with Kani 0.67.0 rather than a
stub, because the guarantee under test is Cargo's own precedence rule: a
`CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` value beats a profile table, while a
package-scoped override beats the variable. One case goes further and verifies
a real harness through the public CLI, which is the only evidence that a
repository selecting a development backend can complete a proof. Set
`PROVER_TOOLS_KANI_067_INSTALLATION` to the installation root to run them.
"""

from __future__ import annotations

import dataclasses as dc
import os
import subprocess
import sys
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.kani_release import KaniExecOptions, prepare_kani_command

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_INSTALLATION = os.environ.get("PROVER_TOOLS_KANI_067_INSTALLATION")

pytestmark = pytest.mark.skipif(
    not _INSTALLATION,
    reason=("set PROVER_TOOLS_KANI_067_INSTALLATION to run the measured Cargo checks"),
)

_PROFILE_CONFIG = (
    "[unstable]\ncodegen-backend = true\n\n"
    '[profile.dev]\ncodegen-backend = "cranelift"\n'
)

_MANIFEST = '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n'

# A manifest profile selection is gated behind Cargo's unstable feature list,
# unlike a configuration-file selection, which the `[unstable]` flag unlocks.
_MANIFEST_PROFILE = (
    'cargo-features = ["codegen-backend"]\n\n'
    f"{_MANIFEST}\n"
    '[profile.dev]\ncodegen-backend = "cranelift"\n'
)

_HARNESS = "#[kani::proof]\nfn check_addition() {\n    assert_eq!(1 + 1, 2);\n}\n"

# The release's declared toolchain identity, which `kani exec` also selects.
_TOOLCHAIN_IDENTITY = "nightly-2025-11-21-x86_64-unknown-linux-gnu"


# Caller state that would change the compiler invocation being measured.
_IGNORED = frozenset({
    "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTDOC",
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_BUILD_RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_RUSTDOC",
    "CARGO_PROFILE_DEV_CODEGEN_BACKEND",
})


def _inherited(tmp_path: Path) -> dict[str, str]:
    """Inherit the caller's shell, isolating Cargo roots and compiler routing."""
    environment = {
        key: value for key, value in os.environ.items() if key not in _IGNORED
    }
    environment["CARGO_HOME"] = str(tmp_path / "cargo-home")
    return environment


def _release_toolchain(environment: dict[str, str]) -> dict[str, str]:
    """Route `cargo` to the selected release's own `rustc`, as the child does.

    Cargo resolves `rustc` through `PATH`, so a measurement built from the
    caller's shell alone would depend on whichever compiler that shell exposes.
    """
    installation = Path(_INSTALLATION or "")
    environment["PATH"] = os.pathsep.join((
        str(installation / "toolchain/bin"),
        environment.get("PATH", ""),
    ))
    environment["RUSTUP_TOOLCHAIN"] = _TOOLCHAIN_IDENTITY
    return environment


@dc.dataclass(frozen=True, slots=True)
class _Build:
    """One measured build: the backend Cargo requested and how it ended."""

    backend: str
    exit_code: int
    output: str


def _write_project(
    root: Path,
    manifest: str = _MANIFEST,
    config: str | None = _PROFILE_CONFIG,
    lib_source: str | None = None,
) -> None:
    """Create a minimal crate, optionally selecting a backend in Cargo config.

    A library source replaces the binary target because a Kani harness cannot
    live in a crate root that also needs a `main` function.
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(manifest, encoding="utf-8")
    if lib_source is None:
        (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    else:
        (root / "src" / "lib.rs").write_text(lib_source, encoding="utf-8")
    if config is None:
        return
    (root / ".cargo").mkdir(parents=True, exist_ok=True)
    (root / ".cargo" / "config.toml").write_text(config, encoding="utf-8")


def _cargo(root: Path, environment: cabc.Mapping[str, str]) -> _Build:
    """Build once and report the backend Cargo passed to rustc and its status."""
    installation = Path(_INSTALLATION or "")
    merged = {**environment, "CARGO_HOME": str(root / "cargo-home")}
    completed = subprocess.run(
        (str(installation / "toolchain/bin/cargo"), "-v", "build"),
        cwd=root,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    output = completed.stdout + completed.stderr
    # `-v` renders the flag as two tokens: `-Z` then `codegen-backend=<name>`.
    marker = "codegen-backend="
    for token in output.split():
        if token.startswith(marker):
            return _Build(token[len(marker) :], completed.returncode, output)
    msg = f"Cargo did not report a codegen backend:\n{output}"
    raise AssertionError(msg)


def _assert_shadowed(root: Path, environment: cabc.Mapping[str, str]) -> None:
    """Require a successful build that used the LLVM backend."""
    build = _cargo(root, environment)
    assert build.exit_code == 0, build.output
    assert build.backend == "llvm", build.output


@pytest.mark.parametrize("declared", ["cranelift", "sentinel-backend"])
def test_profile_selection_is_shadowed_by_the_prepared_child(
    tmp_path: Path, declared: str
) -> None:
    """The child environment, not the repository, decides the backend."""
    _write_project(
        tmp_path,
        _MANIFEST,
        f"[unstable]\ncodegen-backend = true\n\n"
        f'[profile.dev]\ncodegen-backend = "{declared}"\n',
    )
    environment = _inherited(tmp_path)

    spec = prepare_kani_command(
        KaniExecOptions(Path(_INSTALLATION or ""), tmp_path, ("list",)), environment
    )

    assert spec.env is not None, "a prepared command must carry a child environment"
    assert spec.env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm", (
        "the child must pin the development profile to LLVM"
    )
    _assert_shadowed(tmp_path, spec.env)


def test_manifest_profile_selection_is_shadowed_by_the_prepared_child(
    tmp_path: Path,
) -> None:
    """A backend selected in the workspace manifest is overridden as well."""
    _write_project(tmp_path, _MANIFEST_PROFILE, config=None)
    environment = _inherited(tmp_path)

    spec = prepare_kani_command(
        KaniExecOptions(Path(_INSTALLATION or ""), tmp_path, ("list",)), environment
    )

    assert spec.env is not None, "a prepared command must carry a child environment"
    assert spec.env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm", (
        "the child must pin the manifest profile to LLVM"
    )
    _assert_shadowed(tmp_path, spec.env)


def test_package_override_is_not_shadowed_by_the_child(tmp_path: Path) -> None:
    """A package override wins over the variable, which is why it is refused."""
    _write_project(
        tmp_path,
        f'{_MANIFEST}\n[profile.dev.package.probe]\ncodegen-backend = "cranelift"\n',
    )

    environment = _inherited(tmp_path)
    environment["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] = "llvm"
    _release_toolchain(environment)

    build = _cargo(tmp_path, environment)
    # The negative measurement is the backend choice, not build success: the
    # selected Cranelift backend is not installed, so compilation must fail.
    assert build.exit_code != 0, build.output
    assert build.backend == "cranelift", build.output


@pytest.mark.timeout(900)
def test_cabochon_configuration_verifies_a_real_harness(tmp_path: Path) -> None:
    """The exact development-backend configuration still verifies a proof.

    This is an acceptance test, not a mechanism test: Kani 0.67 supplies its
    own `-Zcodegen-backend` for the harness build and tolerates an unknown
    name, so it keeps verifying even when the child override is removed. The
    measurement cases above are what pin the override itself.
    """
    _write_project(tmp_path, lib_source=_HARNESS)
    environment = _release_toolchain(_inherited(tmp_path))

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "kani",
            "exec",
            "--installation",
            str(_INSTALLATION),
            "--cwd",
            str(tmp_path),
            "--extra-arg=--harness",
            "--extra-arg=check_addition",
        ),
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=840,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "VERIFICATION:- SUCCESSFUL" in output, output
