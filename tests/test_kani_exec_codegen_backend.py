"""Opt-in evidence that the selected release shadows a development backend.

These cases exercise the real Cargo shipped with Kani 0.67.0 rather than a
stub, because the guarantee under test is Cargo's own precedence rule: a
`CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` value beats a profile table, while a
package-scoped override beats the variable. Set
`PROVER_TOOLS_KANI_067_INSTALLATION` to the installation root to run them.
"""

from __future__ import annotations

import os
import subprocess
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


def _write_project(root: Path, manifest: str, config: str = _PROFILE_CONFIG) -> None:
    """Create a minimal crate whose Cargo configuration selects Cranelift."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / ".cargo").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(manifest, encoding="utf-8")
    (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (root / ".cargo" / "config.toml").write_text(config, encoding="utf-8")


def _cargo(root: Path, environment: cabc.Mapping[str, str]) -> str:
    """Build once and return the backend Cargo passed to rustc."""
    installation = Path(_INSTALLATION or "")
    merged = {**environment, "CARGO_HOME": str(root / "cargo-home")}
    completed = subprocess.run(
        (str(installation / "toolchain/bin/cargo"), "-v", "build"),
        cwd=root,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    # `-v` renders the flag as two tokens: `-Z` then `codegen-backend=<name>`.
    marker = "codegen-backend="
    for token in output.split():
        if token.startswith(marker):
            return token[len(marker) :]
    msg = f"Cargo did not report a codegen backend:\n{output}"
    raise AssertionError(msg)


@pytest.mark.parametrize("declared", ["cranelift", "sentinel-backend"])
def test_profile_selection_is_shadowed_by_the_prepared_child(
    tmp_path: Path, declared: str
) -> None:
    """The child environment, not the repository, decides the backend."""
    _write_project(
        tmp_path,
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n',
        f"[unstable]\ncodegen-backend = true\n\n"
        f'[profile.dev]\ncodegen-backend = "{declared}"\n',
    )
    environment = _inherited(tmp_path)

    spec = prepare_kani_command(
        KaniExecOptions(Path(_INSTALLATION or ""), tmp_path, ("list",)), environment
    )

    assert spec.env is not None
    assert spec.env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm"
    assert _cargo(tmp_path, spec.env) == "llvm"


def test_package_override_is_not_shadowed_by_the_child(tmp_path: Path) -> None:
    """A package override wins over the variable, which is why it is refused."""
    _write_project(
        tmp_path,
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[profile.dev.package.probe]\ncodegen-backend = "cranelift"\n',
    )

    environment = _inherited(tmp_path)
    environment["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] = "llvm"

    assert _cargo(tmp_path, environment) == "cranelift"
