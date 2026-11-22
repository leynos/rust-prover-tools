"""Prove the audit reads each crate's manifest to decide its kind."""

from __future__ import annotations

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

#: A literal a proc-macro crate may expand into a Kani call.
_LITERAL_SOURCE = 'fn f() { let _ = "kani::assume(x);"; }'


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


def test_the_flag_overrides_an_unreadable_manifest(tmp_path: Path) -> None:
    """An explicit flag decides the crate kind without reading the manifest.

    The override is documented as the way to correct a manifest the audit
    cannot read, so it must come before the manifest is opened.
    """
    write(
        tmp_path,
        {"Cargo.toml": "[package\nname = ", "src/lib.rs": _LITERAL_SOURCE},
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


def test_a_non_string_crate_type_entry_is_refused(tmp_path: Path) -> None:
    """Refuse a manifest whose ``crate-type`` list is not all strings."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[lib]\ncrate-type = [7]\n",
            "src/lib.rs": "fn f() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="non-string entry"):
        audit_tree(tmp_path, ["src/lib.rs"])
