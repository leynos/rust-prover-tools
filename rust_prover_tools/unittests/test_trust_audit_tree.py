"""Prove the module walk follows declarations to the files a crate compiles."""

from __future__ import annotations

import os
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


def symlink(path: Path, target: Path) -> None:
    """Create a symbolic link, skipping the test where the host refuses one.

    An unprivileged Windows runner without Developer Mode raises ``OSError``
    during setup; that is an environment limit, not an audit failure.
    """
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def audited(root: Path, *drivers: str) -> dict[str, list[str]]:
    """Return each audited file's items, keyed by repository-relative path.

    The raw path sequence is checked for duplicates first: a dictionary would
    silently keep the last of two reports for one file.
    """
    files = audit_tree(root, drivers)
    paths = [file.path for file in files]
    assert len(paths) == len(set(paths)), f"duplicate audited files: {paths}"
    return {file.path: [item.item for item in file.items] for file in files}


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


@pytest.mark.parametrize(
    "source",
    [
        '#[path = "other.rs"]\n#[allow(dead_code)]\nmod x;',
        '#[allow(dead_code)]\n#[path = "other.rs"]\nmod x;',
    ],
    ids=["path-first", "unrelated-first"],
)
def test_a_path_attribute_is_found_past_an_unrelated_attribute(
    source: str,
) -> None:
    """Search the whole attribute run, not only the one nearest ``mod``."""
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


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("(", ")"), ("[", "]"), ("{", "}")],
)
def test_a_macro_pattern_in_any_delimiter_is_not_a_declaration(
    opening: str,
    closing: str,
) -> None:
    """Skip the pattern however the ``macro_rules!`` body is bracketed."""
    source = f"macro_rules! m {opening}(mod sample;) => {{}}{closing}\nmod real;"
    assert references(source) == [("real", None, ())]


def test_a_module_named_by_a_macro_metavariable_is_refused() -> None:
    """Refuse ``mod $name;`` in an expansion the scan cannot expand.

    The invocation chooses the file, so the audit cannot tell which source the
    crate compiles.  Missing the declaration would report a clean scan of code
    it never read.
    """
    source = "macro_rules! decl { ($n:ident) => { mod $n; }; }"
    with pytest.raises(TrustAuditError, match="named by a metavariable"):
        references(source)


def test_a_literal_module_declaration_in_an_expansion_is_reported() -> None:
    """Keep a declaration an expansion spells, attributed to its invocation."""
    source = "macro_rules! decl { () => { mod proofs; }; }\ndecl!();"
    assert references(source) == [("proofs", None, ())]


def test_the_first_of_two_path_attributes_is_the_file_compiled() -> None:
    """Follow the attribute ``rustc`` compiles when a declaration carries two.

    ``rustc`` takes the first ``#[path]`` in source order and warns that later
    ones are unused, so reading the attribute nearest the declaration would
    audit a file the crate does not compile.
    """
    source = '#[path = "first.rs"]\n#[path = "second.rs"]\nmod x;'
    assert references(source) == [("x", "first.rs", ())]


def test_a_unicode_module_name_names_its_path_file() -> None:
    """Keep the whole name so a ``#[path]`` module is not silently skipped."""
    source = '#[path = "coffee.rs"]\nmod café;'
    assert references(source) == [("café", "coffee.rs", ())]


@pytest.mark.parametrize("spelling", [r"\x63", r"\u{63}", r"\u{6_3}"])
def test_a_path_escape_is_read_as_rust_reads_it(spelling: str) -> None:
    r"""Decode every spelling of ``c`` rather than reading the escape itself.

    Rust accepts the cooked ``\x63``, the braced ``\u{63}`` and the
    underscore-separated ``\u{6_3}``, and all three name the same file.
    """
    source = f'#[path = "{spelling}hild.rs"]\nmod child;'
    assert references(source) == [("child", "child.rs", ())]


@pytest.mark.parametrize(
    ("spelling", "pattern"),
    [
        (r"\u{0000063}", "malformed"),
        (r"\u{d800}", "not a Unicode scalar value"),
        (r"\u{_63}", "malformed"),
    ],
)
def test_an_undecodable_path_escape_is_refused(spelling: str, pattern: str) -> None:
    r"""Refuse a ``\u{…}`` escape Rust rejects rather than guessing a path.

    Seven digits exceed Rust's six-digit limit, and a surrogate is not a code
    point Rust will compile.
    """
    source = f'#[path = "{spelling}.rs"]\nmod x;'
    with pytest.raises(TrustAuditError, match=pattern):
        references(source)


def test_a_conditional_path_is_refused() -> None:
    """Refuse a declaration whose file a configuration decides."""
    source = '#[cfg_attr(unix, path = "unix_impl.rs")]\nmod platform;'
    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_conditional_path_above_a_direct_path_is_refused() -> None:
    """Examine the whole attribute run, whichever order the two appear in."""
    source = (
        '#[cfg_attr(feature = "alt", path = "alt.rs")]\n#[path = "base.rs"]\nmod child;'
    )
    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


def test_a_cfg_attr_predicate_naming_path_is_not_a_path_attribute() -> None:
    """Read ``path`` in the predicate as a configuration name, not a target."""
    source = '#[cfg_attr(path = "audit", allow(dead_code))]\nmod x;'
    assert references(source) == [("x", None, ())]


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


#: A literal a proc-macro crate may expand into a Kani call.
_LITERAL_SOURCE: typ.Final[str] = 'fn f() { let _ = "kani::assume(x);"; }'


def test_a_proc_macro_manifest_makes_literals_code(tmp_path: Path) -> None:
    """Read the crate kind from the manifest Cargo declares it in."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": ["assume"]}


def test_an_ordinary_manifest_leaves_literals_alone(tmp_path: Path) -> None:
    """An ordinary library's literals are trivia, not the crate's code.

    The manifest is present and readable but declares no proc-macro library,
    which is the case the flag must not turn on by default.
    """
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": []}


def test_the_nearest_manifest_decides_the_crate_kind(tmp_path: Path) -> None:
    """A workspace manifest above a member does not describe the member."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[workspace]\nmembers = ["macros"]\n',
            "macros/Cargo.toml": '[package]\nname = "macros"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "macros/src/lib.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "macros/src/lib.rs") == {
        "macros/src/lib.rs": ["assume"],
    }


def test_a_file_with_no_manifest_above_it_reads_as_ordinary(tmp_path: Path) -> None:
    """A source outside any Cargo package belongs to no proc-macro crate."""
    write(tmp_path, {"src/lib.rs": _LITERAL_SOURCE})

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": []}


def test_a_deprecated_proc_macro_key_is_honoured(tmp_path: Path) -> None:
    """Read Cargo's deprecated underscore spelling as the same flag.

    Cargo accepts ``proc_macro = true`` before the 2024 edition, so the crate
    still builds as a proc-macro crate; missing the flag would leave the code
    its literals generate unread.
    """
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[lib]\nproc_macro = true\n",
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": ["assume"]}


def test_a_proc_macro_crate_type_makes_literals_code(tmp_path: Path) -> None:
    """Read the general crate-type field Cargo accepts for a proc macro.

    ``[lib] crate-type = ["proc-macro"]`` names the same crate type as the
    dedicated flag, so a manifest written that way still expands literals.
    """
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            '[lib]\ncrate-type = ["proc-macro"]\n',
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": ["assume"]}


def test_a_manifest_symlinked_outside_the_root_is_refused(tmp_path: Path) -> None:
    """Refuse a manifest whose target leaves the repository.

    ``is_file`` follows a symbolic link, so probing before resolving would read
    a file the root does not own and decide the crate kind from it.
    """
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-Cargo.toml"
    outside.write_text(
        '[package]\nname = "p"\nversion = "0.1.0"\n\n[lib]\nproc-macro = true\n',
        encoding="utf-8",
    )
    write(repo, {"src/lib.rs": "fn f() {}"})
    symlink(repo / "Cargo.toml", outside)

    with pytest.raises(TrustAuditError, match="resolves outside the repository"):
        audit_tree(repo, ["src/lib.rs"])


def test_a_shared_module_takes_the_drivers_crate_kind(tmp_path: Path) -> None:
    """Scan a ``#[path]`` module as part of the crate that compiles it.

    The shared file sits in a directory with no manifest of its own; reading
    the nearest manifest above it would call the crate ordinary and leave the
    literal's expansion unread.
    """
    write(
        tmp_path,
        {
            "macros/Cargo.toml": '[package]\nname = "macros"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "macros/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod helper;',
            "shared/helper.rs": _LITERAL_SOURCE,
        },
    )

    assert audited(tmp_path, "macros/src/lib.rs") == {
        "macros/src/lib.rs": [],
        "shared/helper.rs": ["assume"],
    }


def test_an_unparseable_manifest_is_refused(tmp_path: Path) -> None:
    """A manifest the audit cannot read leaves the crate kind unknown."""
    write(
        tmp_path,
        {"Cargo.toml": "[package\nname = ", "src/lib.rs": "fn f() {}"},
    )

    with pytest.raises(TrustAuditError, match="the crate kind is unknown"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_the_flag_overrides_the_manifest(tmp_path: Path) -> None:
    """An explicit flag states the crate kind instead of reading the manifest."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    forced = {
        file.path: [item.item for item in file.items]
        for file in audit_tree(tmp_path, ["src/lib.rs"], procedural_macro=True)
    }
    suppressed = {
        file.path: [item.item for item in file.items]
        for file in audit_tree(
            tmp_path,
            ["src/lib.rs"],
            procedural_macro=False,
        )
    }

    assert forced == {"src/lib.rs": ["assume"]}
    assert suppressed == {"src/lib.rs": []}


def test_a_nested_cfg_attr_predicate_is_not_a_path_attribute() -> None:
    """Read a nested ``cfg_attr`` predicate as configuration, not as ``path``.

    ``cfg_attr(path = "fast", allow(dead_code))`` configures by a custom key
    named ``path`` and applies only ``allow``, so the declaration still uses
    the default module file and must not be refused.
    """
    source = '#[cfg_attr(unix, cfg_attr(path = "fast", allow(dead_code)))]\nmod child;'

    assert references(source) == [("child", None, ())]


def test_a_nested_cfg_attr_payload_can_still_select_a_path() -> None:
    """Refuse a nested payload that applies a ``path`` attribute."""
    source = (
        '#[cfg_attr(unix, cfg_attr(feature = "fast", path = "alt.rs"))]\nmod child;'
    )

    with pytest.raises(TrustAuditError, match="conditional #\\[path\\]"):
        references(source)


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


def test_false_overrides_a_proc_macro_manifest(tmp_path: Path) -> None:
    """State that literals are not code even when the manifest makes them so.

    The suppression direction must beat the manifest, not merely agree with an
    ordinary one; otherwise a caller correcting a wrong crate kind would change
    nothing.
    """
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "src/lib.rs": _LITERAL_SOURCE,
        },
    )

    result = audit_tree(tmp_path, ["src/lib.rs"], procedural_macro=False)

    assert {file.path: [item.item for item in file.items] for file in result} == {
        "src/lib.rs": [],
    }


def test_an_array_bracket_before_a_path_attribute_keeps_the_value() -> None:
    """Stop the attribute walk at non-attribute code, keeping what it found."""
    source = 'type Block = [u8; 4];\n#[path = "selected.rs"]\nmod child;'

    assert references(source) == [("child", "selected.rs", ())]


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


def test_a_later_procedural_crate_rescans_a_shared_file(tmp_path: Path) -> None:
    """Apply the stronger policy when a second crate scans the same file.

    The plain driver is first in the stack order, so the shared literal is
    initially read as trivia; the proc-macro arrival must rescan it.
    """
    write(
        tmp_path,
        {
            "plain/Cargo.toml": '[package]\nname = "plain"\nversion = "0.1.0"\n',
            "proc/Cargo.toml": '[package]\nname = "proc"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "plain/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod h;',
            "proc/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod h;',
            "shared/helper.rs": _LITERAL_SOURCE,
        },
    )

    result = audited(tmp_path, "proc/src/lib.rs", "plain/src/lib.rs")

    assert result["shared/helper.rs"] == ["assume"]


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


def test_a_policy_upgrade_reaches_child_modules(tmp_path: Path) -> None:
    """Rescan a shared module's children under the stronger policy.

    The shared module is first reached from a plain crate; its child is
    discovered then.  The later proc-macro arrival upgrades the parent, and the
    child must be re-enqueued so its literals are read as code.
    """
    write(
        tmp_path,
        {
            "plain/Cargo.toml": '[package]\nname = "plain"\nversion = "0.1.0"\n',
            "proc/Cargo.toml": '[package]\nname = "proc"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "plain/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod h;',
            "proc/src/lib.rs": '#[path = "../../shared/helper.rs"]\nmod h;',
            "shared/helper.rs": '#[path = "child.rs"]\nmod child;',
            "shared/child.rs": _LITERAL_SOURCE,
        },
    )

    result = audited(tmp_path, "proc/src/lib.rs", "plain/src/lib.rs")

    assert result["shared/child.rs"] == ["assume"]
