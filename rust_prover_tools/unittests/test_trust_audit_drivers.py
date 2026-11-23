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
    (tmp_path / "bin").mkdir()
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


def test_a_pathless_bench_target_is_inferred_from_benches(tmp_path: Path) -> None:
    """Map the ``bench`` kind to Cargo's ``benches`` directory."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bench]]\nname = "speed"\n',
            "benches/speed.rs": "fn main() {}",
        },
    )

    assert expand_drivers(tmp_path, [tmp_path]) == ("benches/speed.rs",)


def test_an_inherited_edition_keeps_auto_discovery_enabled(
    tmp_path: Path,
) -> None:
    """Resolve ``edition.workspace`` before applying the 2015 defaults."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[workspace]\nmembers = ["member"]\n\n'
            '[workspace.package]\nedition = "2024"\n',
            "member/Cargo.toml": '[package]\nname = "m"\nversion = "0.1.0"\n'
            "edition.workspace = true\n\n"
            '[[bin]]\nname = "main"\npath = "src/main.rs"\n',
            "member/src/main.rs": "fn main() {}",
            "member/src/bin/other.rs": "fn main() {}",
        },
    )

    assert expand_drivers(tmp_path, [tmp_path / "member"]) == (
        "member/src/bin/other.rs",
        "member/src/main.rs",
    )


def test_an_unresolvable_inherited_edition_is_refused(tmp_path: Path) -> None:
    """Refuse discovery when an inherited edition has no workspace manifest."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n'
            "edition.workspace = true\n",
            "src/lib.rs": "fn clean() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="inherits its edition"):
        expand_drivers(tmp_path, [tmp_path])
