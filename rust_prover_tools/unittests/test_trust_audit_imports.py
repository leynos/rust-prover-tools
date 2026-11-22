"""Prove the trust scan follows every name a file's imports bind."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError
from rust_prover_tools.unittests.trust_audit_scan_support import items


def test_a_declared_module_qualifies_a_renamed_member() -> None:
    """Report a renamed import reached through a module the file declares.

    ``mod m { pub use kani::assume as a; }`` puts the binding in this file, so
    ``m::a(..)`` is the same call as the bare ``a(..)``.
    """
    source = "mod m { pub use kani::assume as a; }\nfn f() { m::a(true); }"

    assert items(source) == [("assume", 1), ("assume", 2)]


def test_an_undeclared_module_does_not_qualify_a_renamed_member() -> None:
    """Leave a path through a module this file never declares alone."""
    source = "use kani::assume as a;\nfn f() { elsewhere::a(true); }"

    assert items(source) == [("assume", 1)]


def test_a_plain_member_import_seeds_a_requalified_rename() -> None:
    """Follow ``self::`` to a member imported without a rename.

    ``use kani::assume; use self::assume as a;`` binds ``a`` to Kani's
    ``assume``, so a later ``a(..)`` is trust.
    """
    source = "use kani::assume;\nuse self::assume as a;\nfn f() { a(true); }"

    assert [item for item, _ in items(source)] == ["assume", "assume", "assume"]


def test_a_crate_alias_requalified_through_self_is_followed() -> None:
    """Follow a crate alias renamed again through a local qualifier.

    ``use kani as k; use self::k as kk;`` makes ``kk::assume`` the Kani call.
    """
    source = "use kani as k;\nuse self::k as kk;\nfn f() { kk::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_grouped_plain_import_seeds_every_member() -> None:
    """Seed each unrenamed leaf of a grouped import, not only the last.

    ``use kani::{assume, any};`` binds both names, so a later
    ``use self::assume as a;`` reaches Kani's ``assume``.
    """
    source = "\n".join(
        (
            "use kani::{assume, any_where};",
            "use self::assume as a;",
            "use self::any_where as w;",
            "fn f() { a(true); w(|_| true); }",
        ),
    )

    assert items(source) == [
        ("assume", 1),
        ("any_where", 1),
        ("assume", 2),
        ("any_where", 3),
        ("assume", 4),
        ("any_where", 4),
    ], "both unrenamed imports must survive their later aliases"


def test_a_crate_alias_qualified_through_self_is_followed() -> None:
    """Resolve the crate alias in ``self::k::assume`` before the member.

    ``use kani as k; use self::k::assume as a;`` binds ``a`` to Kani's
    ``assume`` just as ``use k::assume as a;`` would.
    """
    source = "use kani as k;\nuse self::k::assume as a;\nfn f() { a(true); }"

    assert items(source) == [("assume", 2), ("assume", 3)]


def test_a_turbofish_call_to_a_renamed_member_is_reported() -> None:
    """Treat ``::<`` as explicit generic arguments, not a path separator.

    ``a::<u32, _>(..)`` calls the imported item, so its position is a trust
    use even though the item is renamed.
    """
    source = "use kani::any_where as a;\nfn f() { a::<u32, _>(|_| true); }"

    assert items(source) == [("any_where", 1), ("any_where", 2)]


def test_a_crate_alias_through_a_declared_module_is_followed() -> None:
    """Follow a crate alias re-exported by a module this file declares."""
    source = "\n".join(
        (
            "mod m { pub use kani as k; }",
            "use m::k as kk;",
            "fn f() { kk::assume(true); }",
        ),
    )

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_renamed_import_does_not_seed_its_original_name() -> None:
    """Bind only the new name, as Rust does.

    ``use kani::assume as a;`` does not also bind ``assume``, so a later
    ``use self::assume as b;`` must not be read as a rename of Kani's item.
    """
    source = "\n".join(
        (
            "use kani::assume as a;",
            "use self::assume as b;",
            "fn f() { b(true); }",
        ),
    )

    assert items(source) == [("assume", 1)]


def test_a_crate_alias_through_repeated_super_segments_is_followed() -> None:
    """Skip every leading ``super`` before the crate alias."""
    source = "\n".join(
        (
            "use kani as k;",
            "mod a {",
            "    mod b {",
            "        use super::super::k::assume as c;",
            "        fn f() { c(true); }",
            "    }",
            "}",
        ),
    )

    assert items(source) == [("assume", 4), ("assume", 5)]


def test_a_crate_root_import_is_treated_as_a_possible_alias() -> None:
    """Fail closed on a name imported from the crate root.

    The declaring file is outside this per-file scan, so the local name is
    reported as a possible trust alias rather than assumed harmless.
    """
    source = "use crate::k;\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_deeper_crate_root_import_is_a_possible_alias() -> None:
    """Treat a deeper crate-root import's local name as a possible alias.

    The module holding ``k`` is outside the file, so the scan cannot rule out
    that ``kk`` re-exports a trust crate; leaving it unreported would hide
    ``kk::assume``.
    """
    source = "use crate::prelude::k as kk;\nfn f() { kk::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


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


def test_a_member_rename_through_a_declared_module_is_followed() -> None:
    """Follow ``use m::a as b;`` to the member binding ``m`` re-exports."""
    source = "\n".join(
        (
            "mod m { pub use kani::assume as a; }",
            "use m::a as b;",
            "fn f() { b(false); }",
        ),
    )

    assert items(source) == [
        ("assume", 1),
        ("assume", 2),
        ("assume", 3),
    ], "the module's import, the second rename and its call must all report"


def test_a_declared_module_alias_qualifies_a_renamed_member() -> None:
    """Follow ``use m as n;`` for a module the file declares."""
    source = "mod m { pub use kani::assume as a; }\nuse m as n;\nfn f() { n::a(true); }"

    assert items(source) == [("assume", 1), ("assume", 3)]


def test_a_super_item_import_is_a_possible_alias() -> None:
    """Treat a child module's ``use super::k;`` as a possible crate alias."""
    source = "use super::k;\nfn f() { k::assume(true); }"

    assert [item for item, _ in items(source)] == ["assume"]


def test_a_renamed_item_wins_over_the_vocabulary_word_it_reuses() -> None:
    """Report the bound item, not the literal word borrowed for its alias.

    ``use kani::assume as any_where;`` makes ``any_where(true)`` a call to
    Kani's ``assume``; reporting the word ``any_where`` would name a different
    surface.
    """
    source = "use kani::assume as any_where;\nfn f() { any_where(true); }"

    assert items(source) == [("assume", 1), ("assume", 2)]


def test_a_trust_glob_reexported_by_a_declared_module_is_reported() -> None:
    """Report a qualified word a declared module's wildcard re-export reaches."""
    source = "mod m { pub use kani::*; }\nfn f() { m::assume(true); }"

    assert items(source) == [("assume", 2)]


def test_a_local_wildcard_import_is_refused() -> None:
    """Refuse a wildcard whose bindings the per-file scan cannot enumerate."""
    source = "use crate::*;\nfn f() { k::assume(true); }"

    with pytest.raises(TrustAuditError, match="wildcard import"):
        items(source)


def test_a_deeper_local_wildcard_import_is_refused() -> None:
    """Refuse a wildcard at any depth of a crate-root import.

    ``use crate::prelude::*`` binds whatever that module re-exports, which may
    include a crate alias the file never names.
    """
    source = "use crate::prelude::*;\nfn f() { k::assume(true); }"

    with pytest.raises(TrustAuditError, match="wildcard import"):
        items(source)


def test_a_module_alias_of_an_alias_is_followed() -> None:
    """Follow a renamed module through a second rename to a fixed point."""
    source = "\n".join(
        (
            "mod m { pub use kani::assume as a; }",
            "use m as n;",
            "use n as q;",
            "fn f() { q::a(false); }",
        ),
    )

    assert items(source) == [("assume", 1), ("assume", 4)]


def test_a_grouped_self_import_reaches_a_crate_alias() -> None:
    """Read ``use self::{k::assume as a};`` through a known crate alias."""
    source = "use kani as k;\nuse self::{k::assume as a};\nfn f() { a(true); }"

    assert [item for item, _ in items(source)] == ["assume", "assume"]


def test_a_grouped_self_import_reaches_a_member_binding() -> None:
    """Read ``use self::{assume as a};`` through a known member binding."""
    source = "use kani::assume;\nuse self::{assume as a};\nfn f() { a(true); }"

    assert items(source) == [
        ("assume", 1),
        ("assume", 2),
        ("assume", 3),
    ], "the plain import, the grouped import and its call must all report"


def test_a_grouped_module_path_reaches_a_member_binding() -> None:
    """Bind the leaf of a grouped module path to the item the file imported."""
    source = "\n".join(
        (
            "mod inner { pub use kani::assume as a; }",
            "use crate::{inner::a as b};",
            "fn f() { b(true); }",
        ),
    )

    assert items(source) == [
        ("assume", 1),
        ("assume", 2),
        ("assume", 3),
    ], "the module's import, the grouped rename and its call must all report"


def test_a_nested_self_group_reaches_a_member_binding() -> None:
    """Recurse into a nested brace rather than reading its first member."""
    source = "\n".join(
        (
            "use kani::assume as a;",
            "use self::{{unrelated as u, a as b}};",
            "fn f() { b(true); }",
        ),
    )

    assert items(source) == [
        ("assume", 1),
        ("assume", 2),
        ("assume", 3),
    ], "the outer import, the nested rename and its call must all report"


def test_a_root_alias_and_a_member_alias_of_one_name_both_survive() -> None:
    """Merge both readings when one local name denotes two bindings.

    ``y`` denotes both the renamed crate and the renamed member, and each
    meaning is exercised at its own call site: losing either reading drops one
    of the two reports below.
    """
    source = "\n".join(
        (
            "use kani as x;",
            "use kani::assume as x;",
            "use x as y;",
            "fn f() {",
            "    y(true);",
            "    y::assume(true);",
            "}",
        ),
    )

    assert items(source) == [
        ("assume", 2),
        ("assume", 3),
        ("assume", 5),
        ("assume", 6),
    ], "the member's rename and both of its surviving meanings must report"
