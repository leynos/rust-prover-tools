"""Prove the trust audit discovers the Cargo targets a directory compiles."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.trust_audit_cli import audit_trust
from rust_prover_tools.unittests.trust_audit_tree_support import symlink, write

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_a_crate_directory_expands_to_its_entry_points(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A crate directory contributes ``src/lib.rs`` as a driver."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/lib.rs": "kani::assume(true);",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "src/lib.rs:1:7: assume\n" in captured.out
    assert "audited 1 file," in captured.out


def test_a_workspace_directory_expands_every_crate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A workspace directory contributes each member crate's entry points."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n',
            "a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\n',
            "a/src/lib.rs": "kani::assume(true);",
            "b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\n',
            "b/src/lib.rs": "verus_builtin::admit();",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "a/src/lib.rs:1:7: assume\n" in captured.out
    assert "b/src/lib.rs:1:16: admit\n" in captured.out
    assert "audited 2 files," in captured.out


def test_a_directory_without_rust_entry_points_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a directory that contributes no driver, rather than audit nothing."""
    repo = tmp_path / "repo"
    (repo / "empty").mkdir(parents=True)

    status = audit_trust([repo / "empty"], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "contains no Rust entry point to audit" in captured.err


def test_a_binary_target_under_src_bin_is_audited(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A crate directory includes every binary under ``src/bin``."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/lib.rs": "fn clean() {}",
            "src/bin/proof.rs": "kani::assume(true);",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "src/bin/proof.rs:1:7: assume\n" in captured.out
    assert "audited 2 files," in captured.out


def test_an_explicit_manifest_target_is_audited(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A crate directory includes the target paths its manifest names."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bin]]\nname = "proof"\npath = "tools/proof.rs"\n',
            "tools/proof.rs": "kani::assume(true);",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "tools/proof.rs:1:7: assume\n" in captured.out
    assert "audited 1 file," in captured.out


def test_an_outside_directory_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory outside the repository root gets the containment diagnostic."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lib.rs").write_text("kani::assume(true);", encoding="utf-8")

    status = audit_trust([outside], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "resolves outside the repository root" in captured.err


def test_a_directory_entry_escaping_through_a_symlink_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An entry that resolves outside the root is refused, not audited."""
    repo = tmp_path / "repo"
    write(repo, {"src/lib.rs": "fn clean() {}"})
    outside = tmp_path / "outside.rs"
    outside.write_text("kani::assume(true);", encoding="utf-8")
    (repo / "tests").mkdir()
    symlink(repo / "tests" / "escape.rs", outside)

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "resolves outside the repository root" in captured.err


def test_src_bin_subdirectory_targets_are_discovered_like_cargo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit ``src/bin/proof/main.rs`` and let the module walk find helpers.

    A sibling ``helper.rs`` is a module of ``main.rs``, not a binary crate of
    its own, so treating it as a driver would resolve its ``mod nested;`` from
    ``proof/helper/`` instead of ``proof/``.
    """
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/bin/proof/main.rs": "mod helper;",
            "src/bin/proof/helper.rs": "mod nested;",
            "src/bin/proof/helper/nested.rs": "fn f() { kani::assume(true); }",
            "src/bin/proof/unreferenced.rs": "fn g() { kani::assume(false); }",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "src/bin/proof/helper/nested.rs:1:16: assume\n" in captured.out
    assert "unreferenced.rs" not in captured.out, (
        "a file no target declares is not compiled"
    )


def test_an_autobins_false_manifest_skips_the_default_binary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Honour ``autobins = false`` rather than auditing an uncompiled file."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n'
            "autobins = false\n",
            "src/lib.rs": "fn clean() {}",
            "src/main.rs": "fn f() { kani::assume(true); }",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "src/main.rs" not in captured.out
    assert "audited 1 file," in captured.out


def test_a_missing_explicit_target_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a manifest target Cargo would fail to build."""
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bin]]\nname = "proof"\npath = "tools/missing.rs"\n',
            "src/lib.rs": "fn clean() {}",
        },
    )

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "tools/missing.rs" in captured.err
    assert "does not exist" in captured.err


def test_a_manifest_link_outside_the_root_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse to read a manifest whose resolved file leaves the repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text('[package]\nname = "p"\nversion = "0.1.0"\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/lib.rs").write_text("fn clean() {}", encoding="utf-8")
    symlink(repo / "Cargo.toml", outside)

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "resolves outside the repository root" in captured.err


def test_a_linked_target_keeps_the_pathname_cargo_compiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolve modules from the target's pathname, not its link target.

    ``bin/entry.rs`` links to ``shared/entry.rs``.  Passing the directory uses
    the pathname ``bin/entry.rs``, so ``mod child;`` compiles ``bin/child.rs``;
    the resolved spelling would read ``shared/child.rs`` instead.
    """
    repo = tmp_path / "repo"
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[[bin]]\nname = "entry"\npath = "bin/entry.rs"\n',
            "shared/entry.rs": "mod child;",
            "bin/child.rs": "fn f() { kani::assume(true); }",
            "shared/child.rs": "fn clean() {}",
        },
    )
    symlink(repo / "bin" / "entry.rs", repo / "shared" / "entry.rs")

    status = audit_trust([repo], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert "bin/child.rs:1:16: assume\n" in captured.out
    assert "shared/child.rs" not in captured.out
