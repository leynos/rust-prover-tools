"""Prove driver expansion follows Cargo's target rules under the repository."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, expand_drivers
from rust_prover_tools.unittests.trust_audit_tree_support import symlink, write

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_crate_directory_expands_to_its_cargo_targets(tmp_path: Path) -> None:
    """Expand a crate to its library and auto-discovered binary targets."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/lib.rs": "fn clean() {}",
            "src/bin/proof.rs": "fn f() {}",
        },
    )

    assert expand_drivers(tmp_path, [tmp_path]) == (
        "src/bin/proof.rs",
        "src/lib.rs",
    )


def test_a_multi_file_binary_contributes_only_its_main(tmp_path: Path) -> None:
    """Leave a binary's helper modules to the module walk."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/bin/proof/main.rs": "mod helper;",
            "src/bin/proof/helper.rs": "fn helper() {}",
        },
    )

    assert expand_drivers(tmp_path, [tmp_path]) == ("src/bin/proof/main.rs",)


def test_a_linked_target_keeps_the_pathname_cargo_compiles(
    tmp_path: Path,
) -> None:
    """Return the pathname rather than the link target for module lookup."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bin]]\nname = "entry"\npath = "bin/entry.rs"\n',
            "shared/entry.rs": "mod child;",
        },
    )
    symlink(tmp_path / "bin" / "entry.rs", tmp_path / "shared" / "entry.rs")

    assert expand_drivers(tmp_path, [tmp_path]) == ("bin/entry.rs",)


def test_a_directory_without_a_target_is_refused(tmp_path: Path) -> None:
    """Refuse an expansion that would audit nothing."""
    (tmp_path / "empty").mkdir()

    with pytest.raises(TrustAuditError, match="contains no Rust entry point"):
        expand_drivers(tmp_path, [tmp_path / "empty"])


def test_a_missing_explicit_target_is_refused(tmp_path: Path) -> None:
    """Refuse a manifest target Cargo would fail to build."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bin]]\nname = "proof"\npath = "tools/missing.rs"\n',
            "src/lib.rs": "fn clean() {}",
        },
    )

    with pytest.raises(TrustAuditError, match=r"tools/missing\.rs"):
        expand_drivers(tmp_path, [tmp_path])
