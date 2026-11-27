"""The `kani-snapshot-verify` recipe's input guards.

The target builds a throwaway virtual environment and runs a real consumer
install, so it must refuse an unusable input *before* any of that. Each guard
is exercised here with `make` itself: `uv` is stubbed to record any
invocation, so a guard that let its input through would reach the stub and
fail rather than silently doing work.

The recipe is invoked directly rather than through the guide's example. The
example's own contract — that its three assignments reach `make` as values —
belongs to `tests/test_documented_examples.py`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import typing as typ

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

MAKE = shutil.which("make")
NEEDS_MAKE = pytest.mark.skipif(MAKE is None, reason="GNU make is required")

if typ.TYPE_CHECKING:
    from pathlib import Path

#: Each guard's name, the inputs that trip it, and the message it must state.
_GUARDS: typ.Final[tuple[tuple[str, dict[str, str], str], ...]] = (
    (
        "asset-dir-unset",
        {"KANI_SNAPSHOT_ASSET_DIR": ""},
        "set KANI_SNAPSHOT_ASSET_DIR",
    ),
    (
        "asset-dir-missing",
        {"KANI_SNAPSHOT_ASSET_DIR": "absent"},
        "KANI_SNAPSHOT_ASSET_DIR is not a directory",
    ),
    ("wheel-unset", {"KANI_SNAPSHOT_WHEEL": ""}, "set KANI_SNAPSHOT_WHEEL"),
    (
        "wheel-missing",
        {"KANI_SNAPSHOT_WHEEL": "absent.whl"},
        "KANI_SNAPSHOT_WHEEL is not a file",
    ),
    (
        "rustc-not-executable",
        {"KANI_SNAPSHOT_RUSTC_1_93": "@non-executable"},
        "KANI_SNAPSHOT_RUSTC_1_93 is not executable",
    ),
)


def _inputs(tmp_path: Path) -> dict[str, str]:
    """Return a complete set of usable inputs, as paths under `tmp_path`.

    Each path is real and each permission satisfies its guard, so the positive
    case reaches `uv`. A refusal case overrides exactly one value.
    """
    assets = tmp_path / "assets"
    assets.mkdir()
    wheel = tmp_path / "tool.whl"
    wheel.write_bytes(b"wheel")
    rustc = tmp_path / "rustc"
    rustc.write_bytes(b"#!/bin/sh\n")
    rustc.chmod(0o755)
    return {
        "KANI_SNAPSHOT_ASSET_DIR": str(assets),
        "KANI_SNAPSHOT_WHEEL": str(wheel),
        "KANI_SNAPSHOT_RUSTC_1_93": str(rustc),
    }


def _non_executable_rustc(tmp_path: Path) -> str:
    """Return a rustc path that exists and is deliberately not executable."""
    rustc = tmp_path / "rustc-not-executable"
    rustc.write_bytes(b"#!/bin/sh\n")
    rustc.chmod(0o644)
    return str(rustc)


def _uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """Write a `uv` stub that records each invocation, and return its log.

    The stub then fails, which stops the recipe at the first real step instead
    of letting it build an environment or start the opt-in suite. Reaching the
    stub at all is the positive evidence; the case asserts nothing about the
    stub's own exit.
    """
    log = tmp_path / "uv-invocations"
    stub = tmp_path / "uv"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 1\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


@NEEDS_MAKE
@pytest.mark.parametrize(
    ("guard", "overrides", "message"),
    _GUARDS,
    ids=[guard for guard, _, _ in _GUARDS],
)
def test_the_verify_recipe_refuses_an_unusable_input(
    tmp_path: Path, guard: str, overrides: dict[str, str], message: str
) -> None:
    """Each recipe guard refuses its input before any environment is built."""
    del guard  # the id parameter is for the test report only
    make = MAKE
    assert make is not None, "GNU make is required"
    stub, log = _uv_stub(tmp_path)
    inputs = _inputs(tmp_path)
    for name, value in overrides.items():
        match value:
            case "":
                inputs[name] = ""
            case "@non-executable":
                inputs[name] = _non_executable_rustc(tmp_path)
            case _:
                inputs[name] = str(tmp_path / value)

    completed = subprocess.run(
        [make, "--no-print-directory", "kani-snapshot-verify", f"UV={stub}"],
        cwd=_REPO_ROOT,
        env={**os.environ, **inputs},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode != 0, "the guard must refuse its input"
    assert message in completed.stderr, completed.stderr
    assert not log.exists(), (
        "a refused input must not reach a virtual-environment build"
    )


@NEEDS_MAKE
def test_usable_inputs_pass_every_guard(tmp_path: Path) -> None:
    """The guards refuse only what is unusable, so valid inputs reach `uv`.

    Without this case a guard that refused everything would still satisfy
    every refusal test above. The environment build is the first step after
    the guards, so reaching `uv` at all is the evidence; the stub then fails,
    which keeps the target from running a real install.
    """
    make = MAKE
    assert make is not None, "GNU make is required"
    stub, log = _uv_stub(tmp_path)

    completed = subprocess.run(
        [make, "--no-print-directory", "kani-snapshot-verify", f"UV={stub}"],
        cwd=_REPO_ROOT,
        env={**os.environ, **_inputs(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert log.exists(), (
        f"valid inputs were refused before reaching uv:\n{completed.stderr}"
    )
    assert " is not a directory:" not in completed.stderr, completed.stderr
    assert " is not a file:" not in completed.stderr, completed.stderr
    assert " is not executable:" not in completed.stderr, completed.stderr
