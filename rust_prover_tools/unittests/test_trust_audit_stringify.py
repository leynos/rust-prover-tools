"""Prove ``stringify!`` text and disabled modules name no file."""

from __future__ import annotations

import pytest

from rust_prover_tools.unittests.trust_audit_tree_support import references


def test_a_macro_inside_stringify_compiles_no_module() -> None:
    """A macro invocation inside ``stringify!`` expands nothing."""
    source = "\n".join(
        (
            "macro_rules! decl { () => { mod hidden; }; }",
            "const S: &str = stringify!(decl!());",
        ),
    )

    assert references(source) == []


def test_a_module_inside_stringify_is_not_a_declaration() -> None:
    """Skip a ``mod`` in ``stringify!``, which compiles none of its argument."""
    source = "stringify!(mod ghost;)\nmod real;"

    assert references(source) == [("real", None, ())]


@pytest.mark.parametrize("spelling", ["std::stringify!", "::std::stringify!"])
def test_a_builtin_qualified_stringify_is_exempt(spelling: str) -> None:
    """Accept the standard library's qualified spelling of the builtin."""
    source = f"{spelling}(mod ghost;)\nmod real;"

    assert references(source) == [("real", None, ())]


def test_a_std_segment_mid_path_does_not_exempt_stringify() -> None:
    """``custom::std::stringify!`` names a user macro, so its mod counts."""
    source = "custom::std::stringify! { mod hidden; }"

    assert references(source) == [("hidden", None, ())]


def test_a_module_disabled_in_every_configuration_names_no_file() -> None:
    """Skip ``#[cfg(any())] mod unused;``, which no build compiles."""
    source = "#[cfg(any())]\nmod unused;\nmod real;"

    assert references(source) == [("real", None, ())]


def test_a_module_under_a_possible_configuration_is_kept() -> None:
    """Keep a declaration a configuration may enable."""
    source = "#[cfg(unix)]\nmod platform;"

    assert references(source) == [("platform", None, ())]


def test_a_disabled_inline_module_contributes_no_reference() -> None:
    """A declaration inside a module no configuration compiles names no file."""
    source = "#[cfg(any())]\nmod unused { mod hidden; }\nmod real;"

    assert references(source) == [("real", None, ())]
