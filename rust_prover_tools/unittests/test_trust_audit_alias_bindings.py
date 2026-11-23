"""Prove the trust scan resolves grouped, aliased and placeholder imports."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError
from rust_prover_tools.unittests.trust_audit_scan_support import items


def test_a_grouped_self_member_binds_its_parent_name() -> None:
    """Bind the parent name of ``use crate::k::{self};`` as a crate alias."""
    source = "use crate::k::{self};\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_grouped_deeper_crate_import_binds_every_member() -> None:
    """Expand ``prelude::{k, j}`` rather than keeping only its last name."""
    source = "use crate::prelude::{k, j};\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_grouped_deeper_crate_import_keeps_renames_and_plain_members() -> None:
    """Read a renamed member and a plain member of the same nested group."""
    source = "\n".join(
        (
            "use crate::prelude::{k as kk, j};",
            "fn f() { kk::assume(true); j::assume(true); }",
        ),
    )

    assert items(source) == [
        ("assume", 2),
        ("assume", 2),
    ], "both the renamed member and the plain member must resolve"


def test_a_nested_grouped_crate_import_binds_every_member() -> None:
    """Expand a group nested one brace deeper under the crate root."""
    source = "use crate::{prelude::{k, j}};\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_nested_wildcard_under_the_crate_root_is_refused() -> None:
    """Refuse a wildcard member of a nested group under the crate root."""
    source = "use crate::{prelude::{k, *}};\nfn f() { k::assume(true); }"

    with pytest.raises(TrustAuditError, match="wildcard import"):
        items(source)


def test_a_grouped_crate_root_import_binds_its_aliases() -> None:
    """Read ``use crate::{k as kk};`` like the ungrouped form."""
    source = "use crate::{k as kk};\nfn f() { kk::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_an_alias_only_import_reaches_renamed_members() -> None:
    """Reach the member rules when the only trust link is an aliased import.

    ``use crate::k;`` makes ``k`` a possible trust alias, so
    ``use k::{assume as a};`` must bind ``a`` even though the file imports no
    literal trust root.
    """
    source = "use crate::k;\nuse k::{assume as a};\nfn f() { a(true); }"

    assert items(source) == [("assume", 2), ("assume", 3)]


def test_a_precise_capture_bound_is_not_an_import() -> None:
    """Leave ``use<kani>`` in an ``impl Trait`` type out of the import roots.

    A bare ``assume`` is reported only when the file imports a trust root, so
    this source detects the bound being mistaken for an import.  The control
    shows the same bare call reported once a real import is present.
    """
    capture = "fn make<kani>() -> impl Sized + use<kani> { assume(true); }"
    control = "use kani;\nfn make() -> impl Sized { assume(true); }"

    assert items(capture) == [], "a capture bound is not an import"
    assert items(control) == [("assume", 2)], "a real import enables the rule"


def test_a_local_qualifier_import_is_not_a_plain_member() -> None:
    """Do not seed ``self`` from ``use kani::{self, assume};``.

    Seeding it would shadow the local-qualifier dispatch, so
    ``use self::assume as a;`` would take the rename path with the item
    ``self`` and lose Kani's ``assume``.
    """
    source = "use kani::{self, assume};\nuse self::assume as a;\nfn f() { a(true); }"

    assert items(source) == [("assume", 1), ("assume", 2), ("assume", 3)]


def test_an_underscore_alias_binds_no_local_name() -> None:
    """``use kani::assume as _;`` imports the item without naming it."""
    source = "use kani::assume as _;\nfn f() { let _ = 0; }"

    assert items(source) == [("assume", 1)], (
        "the import names Kani, but the placeholder is not a later use"
    )


def test_a_deep_self_rename_names_a_member_not_the_crate() -> None:
    """``use kani::nested::{self as n};`` binds ``n`` to ``kani::nested``.

    Treating ``n`` as a synonym for the whole crate would report ``n::assume``
    as a trust path, although it names an item of ``nested``.
    """
    source = "use kani::nested::{self as n};\nfn f() { n::assume(true); }"

    assert items(source) == [], "a member rename is not a crate alias"


def test_a_direct_self_rename_still_names_the_crate() -> None:
    """Keep ``use kani::{self as k};`` as a crate alias for qualified paths."""
    source = "use kani::{self as k};\nfn f() { k::assume(true); }"

    assert items(source) == [("assume", 2)]
