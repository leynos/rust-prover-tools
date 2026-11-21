"""Hold the exact-version registry to its qualified release and its contract."""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log import (
    KANI_067,
    SUPPORTED_VERSIONS,
    UnsupportedKaniGrammarError,
    grammar_for,
    pretty_name,
    supported_versions,
)


def test_only_the_qualified_release_is_registered() -> None:
    """Widening the grammar must be a deliberate act backed by a corpus."""
    assert supported_versions() == ("0.67.0",)
    assert SUPPORTED_VERSIONS == ("0.67.0",)
    assert KANI_067.version == "0.67.0"
    assert KANI_067.grammar_revision == "combobulate-9dd6e3c"


def test_the_registry_forces_the_regular_output() -> None:
    """The library adds the output argument the grammar was written for."""
    assert grammar_for("0.67.0").output_arguments == ("--output-format=regular",)


def test_an_unregistered_release_names_what_is_registered() -> None:
    """A caller must be able to tell an unknown release from a broken log."""
    with pytest.raises(UnsupportedKaniGrammarError) as raised:
        grammar_for("0.68.0")
    assert raised.value.version == "0.68.0"
    assert raised.value.supported == ("0.67.0",)
    assert "this parser reads ['0.67.0']" in str(raised.value)


def test_selector_and_output_spellings_are_forbidden() -> None:
    """Every spelling that would override selection or evidence is listed."""
    forbidden = grammar_for("0.67.0").forbidden_options
    by_long = {option.long: option for option in forbidden}
    for spelling in (
        "--package",
        "--harness",
        "--exact",
        "--workspace",
        "--jobs",
        "--output-format",
        "--output-into-files",
        "--quiet",
    ):
        assert spelling in by_long, f"{spelling} is not forbidden"
    assert by_long["--package"].short == "-p"
    assert by_long["--jobs"].short == "-j"
    assert by_long["--quiet"].short == "-q"
    assert by_long["--package"].category == "selector"
    assert by_long["--output-format"].category == "output"
    assert by_long["--output-format"].takes_value
    assert not by_long["--exact"].takes_value


def test_pretty_names_join_the_module_path_and_function() -> None:
    """Kani's selector uses the module path relative to the crate root."""
    assert pretty_name(("harnesses",), "verified") == "harnesses::verified"
    assert pretty_name(("a", "b"), "c") == "a::b::c"
    assert pretty_name((), "root_harness") == "root_harness"
