"""Hold the lexical module walker to the fixture workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from rust_prover_tools.kani_inventory import InventoryDiscoveryError, collect_proofs
from rust_prover_tools.unittests.kani_inventory_scan_support import write_lib

_WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


def _proofs(relative: str) -> list[tuple[str, bool]]:
    """Return ``(pretty name, conditional)`` pairs for one crate root."""
    return [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(_WORKSPACE / relative, _WORKSPACE)
    ]


def test_alpha_proofs_include_path_and_inline_modules() -> None:
    """A `#[path]` module and an inline module both extend the module path."""
    assert _proofs("alpha/src/lib.rs") == [
        ("harnesses::verified", False),
        ("harnesses::bounded", False),
        ("harnesses::conditional_extra", True),
        ("harnesses::nested::proof", False),
        ("harnesses::failing", False),
        ("path_proofs::via_path", False),
        ("inline::inside", False),
        ("shared::proof", False),
    ]


def test_a_binary_walks_its_own_module_tree() -> None:
    """A bin crate root resolves `mod` beside its source file."""
    assert _proofs("beta/src/main.rs") == [
        ("in_main", False),
        ("helper::in_helper", False),
    ]


def test_an_integration_test_is_its_own_crate_root() -> None:
    """A test target starts at an empty module path."""
    assert _proofs("beta/tests/integration.rs") == [
        ("integration_proof", False),
        ("gated_proof", True),
    ]


def test_a_proof_records_its_source_file_and_line() -> None:
    """The inventory names where each proof was found."""
    proofs = collect_proofs(_WORKSPACE / "alpha/src/lib.rs", _WORKSPACE)
    via_path = next(proof for proof in proofs if proof.function == "via_path")

    assert via_path.source == "alpha/src/extra/path_proofs.rs"
    assert via_path.line >= 1
    assert via_path.attributes == ("kani::proof",)


def test_an_ambiguous_module_file_is_refused() -> None:
    """Rust itself rejects both `thing.rs` and `thing/mod.rs`."""
    with pytest.raises(InventoryDiscoveryError, match="ambiguous"):
        collect_proofs(_WORKSPACE / "ambiguous/src/lib.rs", _WORKSPACE)


def test_a_missing_module_file_is_refused(tmp_path: Path) -> None:
    """A declaration with no file cannot yield selectors."""
    root = write_lib(tmp_path, "mod missing;\n")

    with pytest.raises(InventoryDiscoveryError, match="has no file"):
        collect_proofs(root, tmp_path)


def test_a_missing_path_attribute_file_is_refused(tmp_path: Path) -> None:
    """A `#[path]` registration that does not exist is an error."""
    root = write_lib(tmp_path, '#[path = "gone.rs"]\nmod gone;\n')

    with pytest.raises(InventoryDiscoveryError, match="does not exist"):
        collect_proofs(root, tmp_path)


def test_a_module_cycle_is_refused(tmp_path: Path) -> None:
    """A file reachable twice would produce duplicate selectors."""
    (tmp_path / "src").mkdir()
    first = tmp_path / "src" / "a.rs"
    second = tmp_path / "src" / "b.rs"
    first.write_text('#[path = "b.rs"]\nmod b;\n', encoding="utf-8")
    second.write_text('#[path = "a.rs"]\nmod a;\n', encoding="utf-8")

    with pytest.raises(InventoryDiscoveryError, match=r"cycle|twice"):
        collect_proofs(first, tmp_path)


def test_a_non_kani_cfg_propagates_to_nested_inline_proofs(tmp_path: Path) -> None:
    """A gated file marks every proof it contains as conditional."""
    root = write_lib(
        tmp_path,
        '#[cfg(feature = "x")]\nmod gated;\n',
    )
    (tmp_path / "src" / "gated.rs").write_text(
        "pub mod inner {\n    #[kani::proof]\n    fn nested() {}\n}\n",
        encoding="utf-8",
    )

    proofs = collect_proofs(root, tmp_path)

    assert [(proof.pretty_name, proof.conditional) for proof in proofs] == [
        ("gated::inner::nested", True)
    ]


def test_an_explicit_path_is_relative_to_the_source_directory(
    tmp_path: Path,
) -> None:
    """`#[path]` in foo.rs resolves beside foo.rs, not under foo/."""
    (tmp_path / "src" / "foo").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(tmp_path, "mod foo;\n")
    (tmp_path / "src" / "foo.rs").write_text(
        '#[path = "proofs.rs"]\nmod proofs;\nmod bar;\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "proofs.rs").write_text(
        "#[kani::proof]\nfn via_path() {}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "foo" / "bar.rs").write_text(
        "#[kani::proof]\nfn via_child() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::proofs::via_path",
        "foo::bar::via_child",
    ]


def test_one_file_reached_through_two_paths_keeps_both_identities(
    tmp_path: Path,
) -> None:
    """A shared source file may be two modules, not a cycle."""
    root = write_lib(
        tmp_path,
        '#[path = "shared.rs"]\nmod a;\n#[path = "shared.rs"]\nmod b;\n',
    )
    (tmp_path / "src" / "shared.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "a::h",
        "b::h",
    ]


def test_an_inline_path_override_roots_its_children(tmp_path: Path) -> None:
    """An inline module's `#[path]` roots the modules nested inside it."""
    root = write_lib(
        tmp_path,
        '#[path = "thread_files"]\nmod thread {\n'
        '    #[path = "tls.rs"]\n    mod local_data;\n}\n',
    )
    (tmp_path / "src" / "thread_files").mkdir()
    (tmp_path / "src" / "thread_files" / "tls.rs").write_text(
        "#[kani::proof]\nfn tls() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "thread::local_data::tls"
    ]


def test_visibility_with_whitespace_before_its_restriction_is_skipped(
    tmp_path: Path,
) -> None:
    """`pub (crate)` is the same visibility as `pub(crate)`."""
    root = write_lib(
        tmp_path,
        "#[kani::proof]\npub (crate) fn restricted() {}\n",
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "restricted"
    ]


def test_a_plain_child_of_an_inline_module_is_under_its_name(
    tmp_path: Path,
) -> None:
    """An inline module advances the directory for its plain children."""
    (tmp_path / "src" / "outer").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(tmp_path, "mod outer { mod proofs; }\n")
    (tmp_path / "src" / "outer" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "outer::proofs::h"
    ]


def test_an_inline_path_in_a_non_mod_rs_file_uses_its_child_directory(
    tmp_path: Path,
) -> None:
    """`foo.rs` inline `#[path]` starts under `foo/`."""
    (tmp_path / "src" / "foo" / "inline").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(tmp_path, "mod foo;\n")
    (tmp_path / "src" / "foo.rs").write_text(
        'mod inline { #[path = "proofs.rs"] mod proofs; }\n', encoding="utf-8"
    )
    (tmp_path / "src" / "foo" / "inline" / "proofs.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::inline::proofs::h"
    ]


def test_plain_children_of_an_inline_path_module_use_that_directory(
    tmp_path: Path,
) -> None:
    """A `#[path]` inline module is the directory for its plain children."""
    (tmp_path / "src" / "thread_files").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(
        tmp_path,
        '#[path = "thread_files"]\nmod thread { mod baz; }\n',
    )
    (tmp_path / "src" / "thread_files" / "baz.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "thread::baz::h"
    ]


def test_an_inline_path_in_a_non_mod_rs_file_keeps_the_file_parent_base(
    tmp_path: Path,
) -> None:
    """`foo.rs` resolves an inline `#[path]` from `src`, not from `src/foo`."""
    (tmp_path / "src" / "thread_files").mkdir(parents=True)
    root = tmp_path / "src" / "lib.rs"
    root = write_lib(tmp_path, "mod foo;\n")
    (tmp_path / "src" / "foo.rs").write_text(
        '#[path = "thread_files"]\nmod thread { mod baz; }\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "thread_files" / "baz.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "foo::thread::baz::h"
    ]


def test_a_path_module_resolves_children_beside_its_file(
    tmp_path: Path,
) -> None:
    """`mod child;` in an explicitly located file resolves beside that file."""
    root = write_lib(tmp_path, '#[path = "alternate.rs"]\nmod proofs;\n')
    (tmp_path / "src" / "alternate.rs").write_text("mod child;\n", encoding="utf-8")
    (tmp_path / "src" / "child.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )

    assert [proof.pretty_name for proof in collect_proofs(root, tmp_path)] == [
        "proofs::child::h"
    ]


def test_a_relative_repository_root_still_yields_relative_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative root is normalised before source paths are derived."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "#[kani::proof]\nfn h() {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    (proof,) = collect_proofs(Path("src/lib.rs"), Path())

    assert proof.source == "src/lib.rs"


def test_inner_cfg_applies_to_every_sibling_proof(tmp_path: Path) -> None:
    """An inner `cfg` configures the module, not only the next item."""
    root = write_lib(
        tmp_path,
        "mod proofs {\n"
        '    #![cfg(feature = "proofs")]\n'
        "    #[kani::proof] fn first() {}\n"
        "    #[kani::proof] fn second() {}\n"
        "}\n",
    )

    assert [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(root, tmp_path)
    ] == [("proofs::first", True), ("proofs::second", True)]
