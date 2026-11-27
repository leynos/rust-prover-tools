"""Hold symlink and lexical-path resolution for the module walker."""

from __future__ import annotations

from pathlib import Path

from rust_prover_tools.kani_inventory import collect_proofs


def test_a_symlinked_path_module_resolves_children_lexically(
    tmp_path: Path,
) -> None:
    """A symlink target does not move the child directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "outside").mkdir()
    target = tmp_path / "outside" / "proofs.rs"
    target.write_text("mod child;\n", encoding="utf-8")
    (tmp_path / "src" / "proofs.rs").symlink_to(target)
    (tmp_path / "src" / "child.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )
    root = tmp_path / "src" / "lib.rs"
    root.write_text('#[path = "proofs.rs"]\nmod proofs;\n', encoding="utf-8")

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::child::h"
    ]


def test_a_symlinked_crate_root_keeps_its_lexical_module_directory(
    tmp_path: Path,
) -> None:
    """`mod child;` beside the link wins over one beside its target.

    Resolving the crate root before deriving child paths would move module
    lookup to the target's directory; `_walk_file` resolves only for reading
    and cycle detection.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "lib.rs").write_text(
        "mod child;\n#[kani::proof]\nfn in_root() {}\n",
        encoding="utf-8",
    )
    (shared / "child.rs").write_text(
        "#[kani::proof]\nfn target_child() {}\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "child.rs").write_text(
        "#[kani::proof]\nfn lexical_child() {}\n",
        encoding="utf-8",
    )
    link = source / "lib.rs"
    link.symlink_to(shared / "lib.rs")

    found = [proof.pretty_name for proof in collect_proofs(link, tmp_path)]

    assert found == ["child::lexical_child", "in_root"]


def test_a_directory_symlink_before_a_parent_segment_is_followed(
    tmp_path: Path,
) -> None:
    """`alias/../lib.rs` routes through the symlink, as the OS resolves it.

    `/repo/alias` links to `/repo/nested/dir`, so `alias/../lib.rs` names
    `/repo/nested/lib.rs`, not `/repo/lib.rs`. Normalising the path first
    would inventory the decoy crate.
    """
    nested = tmp_path / "nested"
    (nested / "dir").mkdir(parents=True)
    (nested / "lib.rs").write_text(
        "mod child;\n#[kani::proof]\nfn real() {}\n",
        encoding="utf-8",
    )
    (nested / "child.rs").write_text(
        "#[kani::proof]\nfn real_child() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.rs").write_text(
        "#[kani::proof]\nfn decoy() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "alias").symlink_to(nested / "dir")

    found = [
        proof.pretty_name
        for proof in collect_proofs(tmp_path / "alias" / ".." / "lib.rs", tmp_path)
    ]

    assert found == ["child::real_child", "real"]


def test_the_former_normalising_lookup_would_have_inventoried_the_decoy(
    tmp_path: Path,
) -> None:
    """The regression distinguishes the two implementations, not just a tree.

    `os.path.normpath` collapses `alias/..` to the parent, which reads the
    decoy crate; the lexical lookup reads the real one.
    """
    import os

    nested = tmp_path / "nested"
    (nested / "dir").mkdir(parents=True)
    (nested / "lib.rs").write_text("#[kani::proof]\nfn real() {}\n", encoding="utf-8")
    (tmp_path / "lib.rs").write_text(
        "#[kani::proof]\nfn decoy() {}\n", encoding="utf-8"
    )
    (tmp_path / "alias").symlink_to(nested / "dir")
    input_path = tmp_path / "alias" / ".." / "lib.rs"

    decoy = [
        proof.pretty_name
        for proof in collect_proofs(Path(os.path.normpath(input_path)), tmp_path)
    ]
    real = [proof.pretty_name for proof in collect_proofs(input_path, tmp_path)]

    assert decoy == ["decoy"]
    assert real == ["real"]
