"""Known ambient coverage flags are isolated only from the selected SDK child."""

from __future__ import annotations

import os
import typing as typ

import pytest

from rust_prover_tools.commands import run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release import KaniExecOptions, prepare_kani_command
from rust_prover_tools.kani_release_flags import verification_flags
from rust_prover_tools.unittests.test_kani_release import (
    installed_release as installed_release,
)
from rust_prover_tools.unittests.test_kani_release import (
    owned_config_discovery as owned_config_discovery,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("source", ["RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"])
@pytest.mark.parametrize(
    "tokens",
    [
        ("-C", "instrument-coverage", "--cfg", "coverage", "--cfg", "coverage_nightly"),
        ("-Cinstrument-coverage", "--cfg=coverage", "--cfg=coverage_nightly"),
        ("-C", "instrument-coverage", "--cfg=coverage", "--cfg", "coverage_nightly"),
        ("-Cinstrument-coverage", "--cfg", "coverage", "--cfg=coverage_nightly"),
    ],
    ids=["paired", "joined", "paired-option-mixed-cfg", "joined-option-mixed-cfg"],
)
def test_isolates_only_known_ambient_coverage_atoms(
    source: str, tokens: tuple[str, ...]
) -> None:
    """Both Cargo flag sources support the exact observed option and bare cfgs."""
    separator = "\x1f" if source == "CARGO_ENCODED_RUSTFLAGS" else " "
    environment = {source: separator.join(tokens)}
    before = dict(environment)
    assert verification_flags(environment) == ("-D", "warnings")
    assert environment == before


@pytest.mark.parametrize(
    "tokens",
    [
        ("-Cinstrument-coverage=yes",),
        ("-Cinstrument-coverage=no",),
        ("-Zinstrument-coverage",),
        ("-Z", "instrument-coverage"),
        ("-Cinstrument-coverage-extra",),
    ],
)
def test_does_not_guess_unqualified_coverage_option_variants(
    tokens: tuple[str, ...],
) -> None:
    """Parameterized, older and similarly named options remain caller policy."""
    assert verification_flags({"RUSTFLAGS": " ".join(tokens)}) == (
        *tokens,
        "-D",
        "warnings",
    )


def test_preserves_custom_cfg_codegen_warning_and_no_target_flags() -> None:
    """Exact bare coverage removal must not erase compatible neighboring settings."""
    compatible = (
        "--cfg=coverage_extra",
        "--cfg",
        'coverage="kept"',
        '--cfg=coverage_nightly="kept"',
        "--cfg=trybuild_no_target",
        "-Copt-level=2",
        "-C",
        "debuginfo=2",
        "-D",
        "warnings",
    )
    environment = {
        "CARGO_ENCODED_RUSTFLAGS": "\x1f".join((
            "-C",
            "instrument-coverage",
            "--cfg=coverage",
            *compatible,
        ))
    }
    assert verification_flags(environment) == compatible


@pytest.mark.parametrize("selection", ["plain", "encoded", "empty-encoded"])
def test_public_preparer_isolates_child_without_mutating_parent_or_wrappers(
    installed_release: Path, tmp_path: Path, selection: str
) -> None:
    """Public preparation emits equivalent spellings and preserves admission state."""
    environment = {
        "CARGO_HOME": str(tmp_path / "cargo-home"),
        "RUSTC_WRAPPER": "/admission rustc",
        "RUSTC_WORKSPACE_WRAPPER": "/workspace-wrapper",
        "RUSTDOC": "/admission rustdoc",
        "RUSTFLAGS": (
            "-C instrument-coverage --cfg=coverage --cfg coverage_nightly "
            "--cfg=trybuild_no_target -Copt-level=2 -D warnings"
        ),
    }
    expected = ("--cfg=trybuild_no_target", "-Copt-level=2", "-D", "warnings")
    if selection == "encoded":
        environment["RUSTFLAGS"] = "--cfg=plain_only"
        environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join((
            "-Cinstrument-coverage",
            "--cfg",
            "coverage",
            "--cfg=coverage_nightly",
            "--cfg=trybuild_no_target",
            "-Copt-level=2",
            "-D",
            "warnings",
        ))
    elif selection == "empty-encoded":
        environment["CARGO_ENCODED_RUSTFLAGS"] = ""
        expected = ("-D", "warnings")
    before = dict(environment)
    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)), environment
    )
    assert spec.env is not None
    assert spec.env["RUSTFLAGS"] == " ".join(expected)
    assert spec.env["CARGO_ENCODED_RUSTFLAGS"] == "\x1f".join(expected)
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"):
        assert spec.env[key] == before[key]
    assert environment == before


@pytest.mark.parametrize("source", ["RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"])
@pytest.mark.parametrize("tokens", [("--cfg",), ("--cfg", "-D", "warnings")])
def test_rejects_incomplete_or_ambiguous_cfg_without_mutating_input(
    source: str, tokens: tuple[str, ...]
) -> None:
    """A cfg value cannot be missing or consume the following compiler option."""
    separator = "\x1f" if source == "CARGO_ENCODED_RUSTFLAGS" else " "
    environment = {source: separator.join(tokens)}
    before = dict(environment)
    with pytest.raises(ProverToolError, match=r"incomplete.*--cfg"):
        verification_flags(environment)
    assert environment == before


@pytest.mark.parametrize("selection", ["plain", "encoded", "empty-encoded"])
def test_real_child_receives_isolated_coverage_flags_and_preserves_failure(
    installed_release: Path, tmp_path: Path, selection: str
) -> None:
    """Cuprum delivers the child-only flag boundary without hiding exit status."""
    driver = installed_release / "bin/kani-driver"
    driver.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$RUSTFLAGS" "$CARGO_ENCODED_RUSTFLAGS" '
        '"$RUSTC_WRAPPER" "$RUSTC_WORKSPACE_WRAPPER" "$RUSTDOC"\nexit 23\n',
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("CARGO_ENCODED_RUSTFLAGS", None)
    environment.update({
        "CARGO_HOME": str(tmp_path / "cargo-home"),
        "RUSTC_WRAPPER": "/test-admission",
        "RUSTC_WORKSPACE_WRAPPER": "/test-workspace-admission",
        "RUSTDOC": "/test-rustdoc",
        "RUSTFLAGS": (
            "-C instrument-coverage --cfg=coverage --cfg coverage_nightly "
            "--cfg=trybuild_no_target -Copt-level=2 -D warnings"
        ),
    })
    expected = ("--cfg=trybuild_no_target", "-Copt-level=2", "-D", "warnings")
    if selection == "encoded":
        environment["RUSTFLAGS"] = "--cfg=ignored_plain"
        environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join((
            "-Cinstrument-coverage",
            "--cfg",
            "coverage",
            "--cfg=coverage_nightly",
            *expected,
        ))
    elif selection == "empty-encoded":
        environment["CARGO_ENCODED_RUSTFLAGS"] = ""
        expected = ("-D", "warnings")
    before = dict(environment)
    ambient_before = dict(os.environ)
    result = run_command(
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ("list",)), environment
        )
    )
    assert result.exit_code == 23
    assert result.stdout.splitlines() == [
        " ".join(expected),
        "\x1f".join(expected),
        "/test-admission",
        "/test-workspace-admission",
        "/test-rustdoc",
    ]
    assert environment == before
    assert os.environ == ambient_before
