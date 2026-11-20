"""Prove the module walk follows declarations to the files a crate compiles."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.trust_audit import TrustAuditError, audit_tree
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.syntax import tokenize

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def references(source: str) -> list[tuple[str, str | None, tuple[str, ...]]]:
    """Return each ``mod`` declaration's name, path attribute and inline chain."""
    return [
        (
            module.name,
            module.path,
            tuple(inline.name for inline in module.enclosing),
        )
        for module in module_references(tokenize(source))
    ]


def write(root: Path, files: cabc.Mapping[str, str]) -> None:
    """Write each source beneath ``root``, creating its parent directory."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def audited(root: Path, *drivers: str) -> dict[str, list[str]]:
    """Return each audited file's items, keyed by repository-relative path."""
    return {
        file.path: [item.item for item in file.items]
        for file in audit_tree(root, drivers)
    }


def test_an_inline_module_body_names_no_file() -> None:
    """Report ``mod x { … }`` as no module reference: its code is already here."""
    assert references("mod x { fn f() {} }") == []


def test_a_bare_module_declaration_names_a_file() -> None:
    """Report the name of ``mod x;`` for the caller to resolve."""
    assert references("mod x;") == [("x", None, ())]


def test_a_path_attribute_names_the_file_directly() -> None:
    """Carry the ``#[path]`` value rather than searching for the module."""
    assert references('#[path = "other.rs"]\nmod x;') == [("x", "other.rs", ())]


def test_a_path_attribute_is_found_past_a_visibility_modifier() -> None:
    """Step over ``pub(crate)``, which may sit between attribute and ``mod``."""
    source = '#[path = "other.rs"]\npub(crate) mod x;'
    assert references(source) == [("x", "other.rs", ())]


def test_a_path_attribute_is_found_past_another_attribute() -> None:
    """Keep walking the attribute run when an earlier one is not ``#[path]``."""
    source = '#[cfg(test)]\n#[path = "other.rs"]\nmod x;'
    assert references(source) == [("x", "other.rs", ())]


def test_a_nested_declaration_carries_its_inline_chain() -> None:
    """Report the inline modules a declaration sits inside, outermost first."""
    source = "mod outer { mod inner { mod x; } }"
    assert references(source) == [("x", None, ("outer", "inner"))]


def test_a_macro_pattern_is_not_a_module_declaration() -> None:
    """Ignore ``mod sample;`` written as the input a macro matches.

    The pattern is a template for source the macro is given, not source the
    crate compiles, so the declaration beside it is the only one reported.
    """
    source = "macro_rules! m { (mod sample;) => {} }\nmod real;"
    assert references(source) == [("real", None, ())]


def test_a_unicode_module_name_names_its_path_file() -> None:
    """Keep the whole name so a ``#[path]`` module is not silently skipped."""
    source = '#[path = "coffee.rs"]\nmod café;'
    assert references(source) == [("café", "coffee.rs", ())]


def test_a_cooked_path_escape_is_read_as_rust_reads_it() -> None:
    r"""Decode ``\x63`` to ``c`` rather than looking for the escape itself."""
    source = '#[path = "\\x63hild.rs"]\nmod child;'
    assert references(source) == [("child", "child.rs", ())]


def test_a_braced_path_escape_is_read_as_rust_reads_it() -> None:
    r"""Decode ``\u{63}`` to ``c`` rather than looking for the escape itself."""
    source = '#[path = "\\u{63}hild.rs"]\nmod child;'
    assert references(source) == [("child", "child.rs", ())]


def test_a_surrogate_path_escape_is_refused() -> None:
    r"""Refuse a ``\u{…}`` escape naming a code point Rust does not allow."""
    source = '#[path = "\\u{d800}.rs"]\nmod x;'
    with pytest.raises(TrustAuditError, match="not a Unicode scalar value"):
        references(source)


def test_a_conditional_path_is_refused() -> None:
    """Refuse a declaration whose file a configuration decides."""
    source = '#[cfg_attr(unix, path = "unix_impl.rs")]\nmod platform;'
    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_declaration_after_an_inline_module_is_at_file_scope() -> None:
    """Leave the chain empty once the inline module's brace has closed."""
    source = "mod outer { mod x; }\nmod y;"
    assert references(source) == [("x", None, ("outer",)), ("y", None, ())]


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
