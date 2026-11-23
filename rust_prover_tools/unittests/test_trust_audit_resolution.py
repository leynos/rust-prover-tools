"""Prove the audit resolves a module from the site that compiles it."""

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


def test_a_windows_path_attribute_derives_a_windows_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""Resolve ``#[path]`` components the way the reading host does.

    On Windows ``r"parts\parent.rs"`` names ``src/parts/parent.rs``, so the
    file's own ``mod child;`` searches ``src/parts/child.rs``.  A POSIX lookup
    would keep the backslash and search ``src/child.rs`` instead.
    """
    from rust_prover_tools.trust_audit import path_values

    monkeypatch.setattr(path_values, "is_windows", lambda: True)
    write(
        tmp_path,
        {
            "src/lib.rs": '#[path = r"parts\\parent.rs"]\nmod parent;',
            "src/parts/parent.rs": "mod child;",
            "src/parts/child.rs": "fn f() { kani::assume(true); }",
            "src/child.rs": "fn clean() {}",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/parts/child.rs": ["assume"],
        "src/parts/parent.rs": [],
    }


def test_an_inherited_macro_use_scope_is_refused(tmp_path: Path) -> None:
    """Refuse a child that invokes a macro its parent imported with macro_use."""
    write(
        tmp_path,
        {
            "src/lib.rs": "#[macro_use]\nmod macros;\nmod child;",
            "src/macros.rs": "macro_rules! emit { () => { mod hidden; }; }",
            "src/child.rs": "emit!();",
        },
    )

    with pytest.raises(TrustAuditError, match="#\\[macro_use\\]"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_first_inline_path_is_relative_to_the_files_directory(
    tmp_path: Path,
) -> None:
    """Resolve an inline ``#[path]`` before the deferred module component.

    rustc applies the attribute to the physical directory of ``src/a.rs``, so
    ``#[path = "parts"]`` there names ``src/parts``.  Searching beneath
    ``src/a/parts`` would read a file the crate never compiles, or refuse a
    valid crate when that directory does not exist.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "mod a;",
            "src/a.rs": '#[path = "parts"]\nmod inner { mod leaf; }',
            "src/parts/leaf.rs": "fn f() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/a.rs": [],
        "src/lib.rs": [],
        "src/parts/leaf.rs": ["assume"],
    }


def test_a_macro_expansion_resolves_from_its_invocation(tmp_path: Path) -> None:
    """Search an expansion's ``mod`` declaration from the invocation site.

    The definition sits at the crate root, but the invocation is inside
    ``mod nested``, so rustc compiles ``nested/child.rs``.  Reading the
    definition's position would search the crate root instead.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "\n".join(
                (
                    "macro_rules! emit {",
                    "    () => { mod child; };",
                    "}",
                    "mod nested {",
                    "    emit!();",
                    "}",
                ),
            ),
            "src/nested/child.rs": "fn f() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/nested/child.rs": ["assume"],
    }


def test_a_macro_expansion_with_no_invocation_compiles_no_file(
    tmp_path: Path,
) -> None:
    """Do not demand a file for a macro that is never invoked here.

    A definition alone compiles nothing, so its ``mod child;`` names no file in
    this scan.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "\n".join(
                (
                    "macro_rules! emit {",
                    "    () => { mod child; };",
                    "}",
                    "fn f() {}",
                ),
            ),
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": []}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows normalizes link/.. before resolving the link",
)
def test_a_driver_through_a_symlinked_directory_names_the_real_file(
    tmp_path: Path,
) -> None:
    """Resolve the driver before collapsing ``..``, as the filesystem does.

    ``src/link`` points at ``src/actual/nested``, so ``src/link/../entry.rs``
    names ``src/actual/entry.rs``.  Collapsing lexically would instead read
    ``src/entry.rs``, which is a different file.
    """
    write(
        tmp_path,
        {
            "src/actual/nested/keep.rs": "fn keep() {}",
            "src/actual/entry.rs": "fn f() { kani::assume(true); }",
            "src/entry.rs": "fn clean() {}",
        },
    )
    symlink(tmp_path / "src/link", tmp_path / "src/actual/nested")

    result = audit_tree(tmp_path, ["src/link/../entry.rs"])

    assert {file.path: [item.item for item in file.items] for file in result} == {
        "src/actual/entry.rs": ["assume"]
    }


def test_a_nested_macro_invocation_resolves_from_the_outer_site(
    tmp_path: Path,
) -> None:
    """Follow a macro that invokes another macro inside its expansion.

    ``inner!`` emits ``mod hidden;``, and ``outer!`` emits the call to
    ``inner!``.  Invoked inside ``mod nested``, the declaration resolves from
    that directory, not from the definition at the crate root.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "\n".join(
                (
                    "macro_rules! inner { () => { mod hidden; }; }",
                    "macro_rules! outer { () => { inner!(); }; }",
                    "mod nested { outer!(); }",
                ),
            ),
            "src/nested/hidden.rs": "fn f() { kani::assume(true); }",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {
        "src/lib.rs": [],
        "src/nested/hidden.rs": ["assume"],
    }


def test_a_symlinked_module_resolves_children_from_its_pathname(
    tmp_path: Path,
) -> None:
    """Derive a child's directory from the pathname the compiler loaded.

    ``src/alias.rs`` points at ``shared/module.rs``; a ``#[path]`` inside it is
    relative to ``src``, not to the link target's directory.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": "mod alias;",
            "shared/module.rs": '#[path = "child.rs"]\nmod child;',
            "src/child.rs": "fn f() { kani::assume(true); }",
            "shared/child.rs": "fn clean() {}",
        },
    )
    symlink(tmp_path / "src/alias.rs", tmp_path / "shared/module.rs")

    result = audited(tmp_path, "src/lib.rs")

    assert result["src/child.rs"] == ["assume"]
    assert "shared/child.rs" not in result


def test_two_resolution_contexts_for_one_file_are_refused(
    tmp_path: Path,
) -> None:
    """Refuse one file reached under two declaring directories.

    ``src/lib.rs`` reaches ``src/foo.rs`` as ``mod foo;`` (owning ``src/foo``
    from directory ``src``) and through a ``#[path]`` symlink (owning
    ``src/foo`` from directory ``src/foo``).  A file-scope ``#[path]`` inside
    the file resolves differently in the two contexts, so neither can be
    chosen.
    """
    write(
        tmp_path,
        {
            "src/lib.rs": 'mod foo;\n#[path = "foo/alias.rs"] mod other;',
            "src/foo.rs": '#[path = "trusted.rs"]\nmod child;',
            "src/foo/trusted.rs": "fn f() {}",
            "src/trusted.rs": "fn g() {}",
        },
    )
    symlink(tmp_path / "src/foo/alias.rs", tmp_path / "src/foo.rs")

    with pytest.raises(TrustAuditError, match="reached both as a file owning"):
        audit_tree(tmp_path, ["src/lib.rs"])


@pytest.mark.parametrize(
    ("drivers", "first"),
    [
        pytest.param(("src/main.rs", "src/lib.rs"), "lib.rs", id="lib-first"),
        pytest.param(("src/lib.rs", "src/main.rs"), "main.rs", id="main-first"),
    ],
)
def test_a_shared_module_is_rescanned_in_either_traversal_order(
    tmp_path: Path,
    drivers: tuple[str, str],
    first: str,
) -> None:
    """Rescan a shared module when a later arrival sees imported macros.

    ``main.rs`` reaches ``shared.rs`` without ``#[macro_use]`` scope and
    defines its own ``emit!``, so the file looks clean.  ``lib.rs`` imports a
    macro with ``#[macro_use]``, and under that scope ``emit!()`` is a macro the
    audit cannot attribute, so the second arrival must be refused rather than
    skipped as already scanned.  ``audit_tree`` pops the last driver first, so
    the ids name the file the traversal actually reads first.
    """
    write(
        tmp_path,
        {
            "src/main.rs": (
                "macro_rules! emit { () => {}; }\nmod shared;\nfn main() { emit!(); }"
            ),
            "src/lib.rs": "#[macro_use]\nmod macros;\nmod shared;",
            "src/macros.rs": "macro_rules! emit { () => { mod hidden; }; }",
            "src/shared.rs": "fn f() { emit!(); }",
        },
    )

    with pytest.raises(TrustAuditError, match="#\\[macro_use\\]"):
        audit_tree(tmp_path, list(drivers))

    assert drivers[-1].endswith(first), "the id names the first driver read"


@pytest.mark.parametrize(
    "drivers",
    [
        ("proc/src/lib.rs", "plain/src/lib.rs"),
        ("plain/src/lib.rs", "proc/src/lib.rs"),
    ],
    ids=["plain-first", "proc-first"],
)
def test_a_macro_scope_rescan_keeps_the_procedural_policy(
    tmp_path: Path,
    drivers: tuple[str, str],
) -> None:
    """A rescan under inherited macro scope does not discard literal findings.

    The proc-macro crate scans the shared module's literals as code.  A later
    arrival from a plain crate needs the inherited macro scope, so the file is
    rescanned; both policies must survive, including for its child module.
    """
    write(
        tmp_path,
        {
            "proc/Cargo.toml": '[package]\nname = "proc"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "proc/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod helper;',
            "plain/Cargo.toml": '[package]\nname = "plain"\nversion = "0.1.0"\n',
            "plain/src/lib.rs": (
                "#[macro_use]\nmod macros;\n"
                '#[path = "../../shared/helper.rs"]\nmod helper;'
            ),
            "plain/src/macros.rs": "macro_rules! emit { () => {}; }",
            "shared/helper.rs": 'fn f() { let _ = "kani::assume(x);"; }\nmod child;',
            "shared/child.rs": 'fn g() { let _ = "kani::assume(y);"; }',
        },
    )

    result = {
        file.path: [item.item for item in file.items]
        for file in audit_tree(tmp_path, list(drivers))
    }

    assert result["shared/helper.rs"] == ["assume"], (
        "the literal finding must survive the macro-scope rescan"
    )
    assert result["shared/child.rs"] == ["assume"], (
        "a child module must inherit both policies from the rescan"
    )


def test_a_driver_spelling_with_a_missing_component_is_refused(
    tmp_path: Path,
) -> None:
    """Require every component of the driver spelling to exist.

    ``src/missing/../lib.rs`` is not a path the compiler can open, although a
    non-strict resolution would fold it into ``src/lib.rs`` and report success.
    """
    write(tmp_path, {"src/lib.rs": "fn clean() {}"})

    with pytest.raises(TrustAuditError, match="cannot be read"):
        audit_tree(tmp_path, ["src/missing/../lib.rs"])


def test_a_nul_in_a_path_attribute_is_refused(tmp_path: Path) -> None:
    """Refuse a decoded ``#[path]`` value no filesystem can hold."""
    write(
        tmp_path,
        {"src/lib.rs": '#[path = "a\\0b.rs"]\nmod child;'},
    )

    with pytest.raises(TrustAuditError, match="NUL"):
        audit_tree(tmp_path, ["src/lib.rs"])


@pytest.mark.parametrize(
    "value",
    ["/outside/absolute.rs", "C:/drive.rs", "C:relative.rs"],
    ids=["posix-absolute", "drive-absolute", "drive-relative"],
)
def test_a_rooted_path_attribute_is_refused(tmp_path: Path, value: str) -> None:
    """Refuse a path value that names a file outside the declaring module."""
    write(
        tmp_path,
        {"src/lib.rs": f'#[path = "{value}"]\nmod child;'},
    )

    with pytest.raises(TrustAuditError, match=r"absolute|drive-qualified"):
        audit_tree(tmp_path, ["src/lib.rs"])
