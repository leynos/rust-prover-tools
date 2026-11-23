"""Prove dependency renames and linked drivers keep their crate kind."""

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

#: A literal a proc-macro crate may expand into a Kani call.
_LITERAL_SOURCE = 'fn f() { let _ = "kani::assume(x);"; }'


def test_a_target_specific_trust_rename_is_refused(tmp_path: Path) -> None:
    """Refuse a trust package renamed inside a target dependency table."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[target.'cfg(unix)'.dependencies]\n"
            'proof = { package = "kani", version = "0.67.0" }\n',
            "src/lib.rs": "fn f() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="renames the Cargo package"):
        audit_tree(tmp_path, ["src/lib.rs"])


def test_a_workspace_inherited_trust_rename_is_refused(tmp_path: Path) -> None:
    """Refuse a member that inherits a renamed trust package from its workspace."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[workspace]\nmembers = ["member"]\n\n'
            "[workspace.dependencies]\n"
            'proof = { package = "kani", version = "0.67.0" }\n',
            "member/Cargo.toml": '[package]\nname = "member"\nversion = "0.1.0"\n\n'
            "[dependencies]\nproof.workspace = true\n",
            "member/src/lib.rs": "fn f() {}",
        },
    )

    with pytest.raises(TrustAuditError, match="renames the Cargo package"):
        audit_tree(tmp_path, ["member/src/lib.rs"])


def test_an_unchanged_package_name_is_not_a_rename(tmp_path: Path) -> None:
    """``vstd = { package = "vstd", … }`` still binds the crate as ``vstd``."""
    write(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n\n'
            "[dependencies]\n"
            'vstd = { package = "vstd", version = "0.1.0" }\n',
            "src/lib.rs": "fn f() {}",
        },
    )

    assert audited(tmp_path, "src/lib.rs") == {"src/lib.rs": []}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows normalizes link/.. before resolving the link",
)
def test_a_linked_driver_keeps_its_owning_crate_kind(tmp_path: Path) -> None:
    """Read the crate kind from the manifest above the pathname Cargo gave.

    ``derive/src/lib.rs`` links to ``shared/lib.rs``; resolving the link first
    would start the manifest search in ``shared`` and miss the proc-macro
    declaration, leaving the crate's literals unread.
    """
    write(
        tmp_path,
        {
            "derive/Cargo.toml": '[package]\nname = "derive"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "shared/lib.rs": _LITERAL_SOURCE,
        },
    )
    (tmp_path / "derive" / "src").mkdir(parents=True)
    symlink(tmp_path / "derive" / "src" / "lib.rs", tmp_path / "shared" / "lib.rs")

    assert audited(tmp_path, "derive/src/lib.rs") == {
        "shared/lib.rs": ["assume"],
    }
