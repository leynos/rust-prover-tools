"""Verification flag routing regressions for the qualified Kani release."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release_flags import verification_flags


@pytest.mark.parametrize(
    "flags",
    [
        "-D warnings -Zthreads=8 -Clink-arg=-fuse-ld=mold -Zcodegen-backend=cranelift",
        (
            "-D warnings -Z threads=8 -C link-arg=-fuse-ld=mold -Z "
            "codegen-backend=cranelift"
        ),
    ],
)
def test_removes_only_development_options(flags: str) -> None:
    """Each supported acceleration form is absent from verifier input."""
    assert verification_flags({"RUSTFLAGS": flags}) == ("-D", "warnings")


def test_encoded_source_wins_and_preserves_compatible_arguments() -> None:
    """Cargo precedence and custom flags survive Kani's plain-string boundary."""
    env = {
        "RUSTFLAGS": "--cfg ignored",
        "CARGO_ENCODED_RUSTFLAGS": "--cfg\x1fkeep\x1f-Lnative=/native",
    }
    assert verification_flags(env) == (
        "--cfg",
        "keep",
        "-Lnative=/native",
        "-D",
        "warnings",
    )
    assert verification_flags({
        "RUSTFLAGS": "--cfg ignored",
        "CARGO_ENCODED_RUSTFLAGS": "",
    }) == ("-D", "warnings")


@pytest.mark.parametrize(
    "flags", ["-C", "-Z", "-Zcodegen-backend=other", "--cfg\x1fwith space", "\udcff"]
)
def test_rejects_unrepresentable_or_ambiguous_flags(flags: str) -> None:
    """Malformed inputs fail before a child can run."""
    with pytest.raises(ProverToolError):
        verification_flags({"CARGO_ENCODED_RUSTFLAGS": flags})


@pytest.mark.parametrize(
    "key", ["CARGO_PROFILE_DEV_CODEGEN_BACKEND", "CARGO_PROFILE_TEST_CODEGEN_BACKEND"]
)
def test_rejects_independent_backend_activation(key: str) -> None:
    """Flag sanitization cannot override independent Cargo profile settings."""
    with pytest.raises(ProverToolError, match="backend"):
        verification_flags({key: "cranelift"})


def test_plain_cfg_quotes_are_compiler_syntax_not_shell_quoting() -> None:
    """Cargo-style tokenization must preserve a quoted cfg value verbatim."""
    assert verification_flags({"RUSTFLAGS": '--cfg feature="custom"'}) == (
        "--cfg",
        'feature="custom"',
        "-D",
        "warnings",
    )


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20)
)
def test_custom_cfg_survives_both_effective_flag_sources(name: str) -> None:
    """Arbitrary compatible cfg identities survive without quote loss or reordering."""
    custom = f'custom_{name}="kept"'
    expected = ("--cfg", custom, "-D", "warnings")
    assert verification_flags({"RUSTFLAGS": f"--cfg {custom} -Zthreads=8"}) == expected
    assert (
        verification_flags({
            "CARGO_ENCODED_RUSTFLAGS": f"--cfg\x1f{custom}\x1f-Zthreads=8"
        })
        == expected
    )
