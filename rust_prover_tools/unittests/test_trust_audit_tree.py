"""Prove the audit walks every module a Rust crate compiles."""

from __future__ import annotations

import os
import typing as typ

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, audit_tree
from rust_prover_tools.unittests.trust_audit_tree_support import (
    audited,
    symlink,
    write,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_the_walk_audits_a_declared_module(tmp_path: Path) -> None:
    """Follow a bare declaration to the file beside the crate root."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod child;\nfn f() { kani::assume(true); }",
            "src/child.rs": "pub fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/child.rs": ["assume"],
        "src/lib.rs": ["assume"],
    }


def test_the_walk_follows_a_nested_module_directory(tmp_path: Path) -> None:
    """Search a non-root module's own directory for its children."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod foo;",
            "src/foo.rs": "mod bar;",
            "src/foo/bar.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/foo.rs": [],
        "src/foo/bar.rs": ["assume"],
        "src/lib.rs": [],
    }


def test_an_integration_test_is_a_crate_root(tmp_path: Path) -> None:
    """Search beside ``tests/proof.rs`` rather than in ``tests/proof/``.

    Every file directly under ``tests/`` is compiled as its own crate root, so
    its bare declarations resolve beside it even though its name is neither
    ``lib.rs`` nor ``main.rs``. A decoy in ``tests/proof/`` would otherwise be
    audited in place of the file the crate actually compiles.
    """
    write(
        tmp_path,
        {
            "tests/proof.rs": "mod helper;\nfn f() { kani::assume(true); }",
            "tests/helper.rs": "fn g() { kani::assume(true); }",
            "tests/proof/helper.rs": "fn decoy() {}",
        },
    )

    assert audited(tmp_path, "tests/proof.rs") == {
        "tests/helper.rs": ["assume"],
        "tests/proof.rs": ["assume"],
    }


@pytest.mark.skipif(os.name == "nt", reason="a backslash separates on Windows")
def test_a_backslash_in_a_path_attribute_names_one_file(tmp_path: Path) -> None:
    r"""Read ``r"a\b.rs"`` as one filename rather than as a directory chain."""
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = r"a\\b.rs"]\nmod x;',
            "src/a\\b.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/a\\b.rs": ["assume"],
        "src/lib.rs": [],
    }


def test_the_walk_prefers_a_module_directory_over_the_declaring_directory(
    tmp_path: Path,
) -> None:
    """Ignore a decoy beside the declaring file when the module has its own."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod foo;",
            "src/foo.rs": "mod bar;",
            "src/bar.rs": "fn decoy() { kani::assume(true); }",
            "src/foo/bar.rs": "fn real() {}",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/foo.rs": [],
        "src/foo/bar.rs": [],
        "src/lib.rs": [],
    }


def test_the_walk_finds_a_module_in_its_own_directory(tmp_path: Path) -> None:
    """Fall back to ``name/mod.rs`` when ``name.rs`` does not exist."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod foo;",
            "src/foo/mod.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/foo/mod.rs": ["assume"],
        "src/lib.rs": [],
    }


def test_a_path_loaded_file_owns_its_own_directory(tmp_path: Path) -> None:
    """Search beside the loaded file, not beneath the module name it replaced.

    ``#[path = "impl.rs"] mod component;`` makes ``src/impl.rs`` the module
    ``component``, but rustc resolves ``mod trusted;`` inside it from the file's
    own directory. Searching ``src/component/`` instead would audit a decoy
    rather than the file the crate compiles.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = "impl.rs"]\nmod component;',
            "src/impl.rs": "mod trusted;",
            "src/trusted.rs": "fn g() { kani::assume(true); }",
            "src/component/trusted.rs": "fn decoy() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/impl.rs": [],
        "src/lib.rs": [],
        "src/trusted.rs": ["assume"],
    }


def test_a_path_loaded_module_owns_its_own_directory(tmp_path: Path) -> None:
    """Search beside a path-loaded file declared inside an inline module."""
    write(
        tmp_path,
        {
            "src/lib.rs": ('mod outer { #[path = "deep/impl.rs"] mod inner; }'),
            "src/outer/deep/impl.rs": "mod child;",
            "src/outer/deep/child.rs": "fn g() { kani::assume(true); }",
            "src/outer/inner/child.rs": "fn decoy() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/outer/deep/child.rs": ["assume"],
        "src/outer/deep/impl.rs": [],
    }


def test_a_main_rs_module_owns_its_directory(tmp_path: Path) -> None:
    """Give ``main.rs`` crate-root treatment only when it is the entry point.

    ``mod main;`` loads ``src/main.rs`` as an ordinary module, so the file owns
    ``src/main/`` for its children. Treating the stem as a crate root would
    audit ``src/child.rs``, which the crate never compiles.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "mod main;",
            "src/main.rs": "mod child;",
            "src/main/child.rs": "fn g() { kani::assume(true); }",
            "src/child.rs": "fn decoy() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/main.rs": [],
        "src/main/child.rs": ["assume"],
    }


def test_a_cyclic_path_through_a_symlink_terminates(tmp_path: Path) -> None:
    """Scan a ``#[path]`` cycle once, under the physical file it reaches.

    The symlink names the declaring file again, so following the raw spelling
    would walk for ever.  The target is canonicalized before it is claimed, and
    the second arrival is the same module.
    """
    write(tmp_path, {"src/lib.rs": '#[path = "alias.rs"]\nmod again;'})
    symlink(tmp_path / "src/alias.rs", tmp_path / "src/lib.rs")

    audited = audit_tree(tmp_path, ["src/lib.rs"])

    assert [file.path for file in audited] == ["src/lib.rs"]


def test_one_file_reached_with_two_owners_is_refused(tmp_path: Path) -> None:
    """Refuse a file whose children would resolve beneath two directories.

    ``mod a;`` makes ``src/a.rs`` own ``src/a``, while the ``#[path]`` names the
    same file as owning ``src``.  Whichever owner were kept, the other set of
    child modules would be missed.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": 'mod a;\n#[path = "a.rs"] mod b;',
            "src/a.rs": "fn f() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="reached both as a file owning"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_the_walk_honours_a_path_attribute(tmp_path: Path) -> None:
    """Read the file a ``#[path]`` attribute names, relative to the declaring file."""
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = "elsewhere/real.rs"]\nmod x;',
            "src/elsewhere/real.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/elsewhere/real.rs": ["assume"],
        "src/lib.rs": [],
    }


def test_the_walk_uses_an_inline_module_directory(tmp_path: Path) -> None:
    """Search beneath an inline module's name for a bare declaration."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod nested { mod child; }",
            "src/nested/child.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/nested/child.rs": ["assume"],
    }


def test_an_inline_path_attribute_replaces_its_directory(tmp_path: Path) -> None:
    """Substitute an inline module's ``#[path]`` for the directory it would own."""
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = "thread_files"]\nmod thread { mod child; }',
            "src/thread_files/child.rs": "fn g() { kani::assume(true); }",
            "src/thread/child.rs": "fn decoy() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/thread_files/child.rs": ["assume"],
    }


def test_an_ambiguous_module_is_refused(tmp_path: Path) -> None:
    """Refuse a module both candidate files could name, as rustc does."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod child;",
            "src/child.rs": "fn f() {}",
            "src/child/mod.rs": "fn g() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="ambiguous"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_missing_module_is_refused(tmp_path: Path) -> None:
    """Refuse source whose declared module resolves to no file."""
    write(tmp_path, {"src/lib.rs": "mod absent;"})

    with pytest.raises(TrustAuditError, match="declares module 'absent'"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_module_target_outside_the_repository_is_refused(tmp_path: Path) -> None:
    """Refuse an escaping ``#[path]`` before asking whether the file exists."""
    write(tmp_path, {"src/lib.rs": '#[path = "../../outside.rs"]\nmod x;'})

    with pytest.raises(TrustAuditError, match="resolves outside the repository"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_missing_driver_is_refused(tmp_path: Path) -> None:
    """Refuse an entry point that cannot be read."""
    with pytest.raises(TrustAuditError, match="cannot be read"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_module_named_twice_is_audited_once(tmp_path: Path) -> None:
    """Keep each file in the report once, however many declarations reach it."""
    write(
        tmp_path,
        {
            "src/lib.rs": "mod a;\nmod b;",
            "src/a.rs": '#[path = "shared.rs"]\nmod shared;',
            "src/b.rs": '#[path = "shared.rs"]\nmod shared;',
            "src/shared.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/a.rs": [],
        "src/b.rs": [],
        "src/lib.rs": [],
        "src/shared.rs": ["assume"],
    }


def test_a_file_reached_twice_with_one_owner_is_audited_once(
    tmp_path: Path,
) -> None:
    """Deduplicate a second declaration that agrees on the file's owner.

    A ``#[path]``-loaded file owns the directory it sits in, so two attributes
    naming it reach the same file under the same owner.  That is ordinary,
    not the ambiguity a differing owner represents.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": (
                '#[path = "common.rs"]\nmod x;\n#[path = "common.rs"]\nmod y;'
            ),
            "src/common.rs": "fn g() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/common.rs": ["assume"],
        "src/lib.rs": [],
    }


def test_a_file_reached_under_two_owners_is_refused(tmp_path: Path) -> None:
    """Refuse one file whose children would resolve beneath two directories.

    ``#[path = "common.rs"]`` loads it as a file owning ``src``, while
    ``mod common;`` loads it as a module owning ``src/common``; the crate
    compiles both, reading their children from different places.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = "common.rs"]\nmod x;\nmod common;',
            "src/common.rs": "fn g() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="different directories"):
        audit_tree(tmp_path, ["src/lib.rs"])
