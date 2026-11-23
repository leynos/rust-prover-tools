"""Prove the trust scan refuses what a macro repetition could hide."""

from __future__ import annotations

import pytest

from rust_prover_tools.trust_audit import TrustAuditError
from rust_prover_tools.unittests.trust_audit_scan_support import items


@pytest.mark.parametrize("path", ["vstd::prelude", "kani::nested"])
def test_a_multisegment_repetition_trust_path_is_refused(path: str) -> None:
    """Walk back to the first segment before deciding a repetition is trusted."""
    source = f"macro_rules! call {{ ($($f:ident)+) => {{ {path}::$($f)+(true) }}; }}"

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_multisegment_nontrust_repetition_is_accepted() -> None:
    """Leave a repetition rooted at a non-trust crate alone."""
    source = "macro_rules! call { ($($f:ident)+) => { other::nested::$($f)+(true) }; }"

    assert items(source) == []


def test_a_multitoken_repetition_separator_is_refused() -> None:
    """Skip a multi-token separator such as ``>>`` before the repetition operator."""
    source = "macro_rules! call { ($($p:ident)>>+) => { $($p)>>+::assume(true) }; }"

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_metavariable_import_rename_is_refused() -> None:
    """Refuse ``use std::$name as load;``, whose bound name the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! wrapper {",
            '    ($name:ident) => { use std::$name as load; load!("hidden.rs"); };',
            "}",
            "wrapper!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_import_alias_inside_a_repetition_is_refused() -> None:
    """Refuse ``use std::{$(include as $name),*};``, a repeated rename."""
    source = "\n".join(
        (
            "macro_rules! bind {",
            "    ($($name:ident),*) => { use std::{$(include as $name),*}; };",
            "}",
            "bind!(load);",
        ),
    )

    with pytest.raises(TrustAuditError, match="local name"):
        items(source)


def test_a_metavariable_rename_after_a_sibling_group_is_refused() -> None:
    """Skip a balanced sibling group rather than stopping at its closing brace."""
    source = "\n".join(
        (
            "macro_rules! bind {",
            "    ($name:ident) => { use std::{fmt::{Debug}, $name as load}; };",
            "}",
            "bind!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_import_rename_inside_a_group_is_refused() -> None:
    """Refuse ``use std::{$name as load};``, a grouped form of the same rename."""
    source = "\n".join(
        (
            "macro_rules! wrapper {",
            '    ($name:ident) => { use std::{$name as load}; load!("hidden.rs"); };',
            "}",
            "wrapper!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="renames an import"):
        items(source)


def test_a_metavariable_glob_root_is_refused() -> None:
    """Refuse ``use $p::*;``, whose bound names only the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! glob { ($p:ident) => { use $p::*; assume(true); }; }",
            "glob!(kani);",
        ),
    )

    with pytest.raises(TrustAuditError, match="glob or grouped"):
        items(source)


def test_a_metavariable_grouped_import_root_is_refused() -> None:
    """Refuse ``use $p::{assume as a};``, whose crate the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! bind { ($p:ident) => { use $p::{assume as a}; }; }",
            "bind!(kani);",
            "fn f() { a(true); }",
        ),
    )

    with pytest.raises(TrustAuditError, match="glob or grouped"):
        items(source)


def test_a_metavariable_import_destination_is_refused() -> None:
    """Refuse ``use std::include as $n;``, whose local name is an argument."""
    source = "\n".join(
        (
            "macro_rules! bind { ($n:ident) => { use std::include as $n; }; }",
            "bind!(load);",
            'load!("hidden.rs");',
        ),
    )

    with pytest.raises(TrustAuditError, match="local name"):
        items(source)


def test_a_metavariable_cast_is_not_an_import_rename() -> None:
    """Leave ``$value as u64`` alone: it is an expression, not an import."""
    source = "\n".join(
        (
            "macro_rules! widen { ($value:expr) => { $value as u64 }; }",
            "fn f() { widen(1); }",
        ),
    )

    assert items(source) == []


def test_a_metavariable_final_path_segment_is_refused() -> None:
    """Refuse ``kani::$f(..)``, whose called item only the arguments decide."""
    source = "macro_rules! call { ($f:ident) => { kani::$f(true) }; }"

    with pytest.raises(TrustAuditError, match="final path segment"):
        items(source)


def test_a_metavariable_in_both_path_segments_is_refused() -> None:
    """Refuse ``$p::$i(..)``, whose called item and crate are both arguments."""
    source = (
        "macro_rules! call { ($p:ident, $i:ident) => { $p::$i(true); }; }\n"
        "fn f() { call!(kani, assume); }"
    )

    with pytest.raises(TrustAuditError, match="metavariables"):
        items(source)


def test_a_trust_qualified_metavariable_path_need_not_be_called() -> None:
    """Refuse ``kani::$f`` even when it names an item rather than calling one."""
    source = "macro_rules! get { ($f:ident) => { kani::$f }; }"

    with pytest.raises(TrustAuditError, match="final path segment"):
        items(source)


def test_a_repetition_wrapped_metavariable_trust_path_is_refused() -> None:
    """Refuse ``kani::$($f)*(..)``, whose item the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! call { ($($f:ident)*) => { kani::$($f)*(true) }; }",
            "call!(size_of);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_repetition_wrapped_metavariable_qualifier_is_refused() -> None:
    """Refuse ``$($p)+::assume(..)``, whose crate the arguments decide."""
    source = "\n".join(
        (
            "macro_rules! call { ($($p:ident)+) => { $($p)+::assume(true) }; }",
            "call!(kani);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_repetition_wrapped_metavariable_invocation_is_refused() -> None:
    """Refuse ``$($m)+!("hidden.rs")``, whose macro the arguments decide."""
    source = "\n".join(
        (
            'macro_rules! load { ($($m:ident)+) => { $($m)+!("hidden.rs"); }; }',
            "load!(include);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_nested_repetition_wrapped_trust_path_is_refused() -> None:
    """Walk every enclosing repetition before deciding what a path is rooted at."""
    source = "\n".join(
        (
            "macro_rules! call {",
            "    ($($($f:ident),+);+) => { kani::$($($f),+);+(true) };",
            "}",
            "call!(assume);",
        ),
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_an_ordinary_repetition_is_accepted() -> None:
    """Leave a repetition that emits no path or invocation alone.

    Only a repetition wrapped around a metavariable that then reaches a trust
    path or an invocation is refused; a plain list expansion is ordinary code.
    """
    source = "\n".join(
        (
            "macro_rules! list { ($($x:ident),*) => { $($x),* }; }",
            "fn f() { list!(a, b); }",
        ),
    )

    assert items(source) == []


def test_a_nested_repetition_with_a_call_is_refused() -> None:
    """Climb past a repetition whose next token is a call, not the group close."""
    source = (
        "macro_rules! call { "
        "($($($f:ident),+ (true))+) => { kani::$($($f),+ (true))+ }; }"
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_an_inner_repetition_invocation_is_refused() -> None:
    """Refuse an inner repetition that invokes a macro chosen by its argument.

    The outer repetition ends before a brace, so only the inner boundary names
    the ``!`` and the macro it invokes.
    """
    source = (
        'macro_rules! load { ($($($m:ident)+);*) => { $( $($m)+!("hidden.rs"); )* }; }'
    )

    with pytest.raises(TrustAuditError, match="repetition-wrapped"):
        items(source)


def test_a_nested_repetition_without_a_trust_path_is_accepted() -> None:
    """Leave a nested repetition that emits no trust path alone."""
    source = "macro_rules! list { ($($($x:ident),+);+) => { $($($x),+);+ }; }"

    assert items(source) == []
