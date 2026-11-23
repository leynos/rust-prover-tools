"""Prove the parser reproduces every recorded corpus fact."""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log import (
    SUPPORTED_KANI_VERSIONS,
)
from rust_prover_tools.unittests.kani_log_parse_support import (
    CORPUS_CASES,
    CURRENT_CORPUS,
    EXPECTED,
    FIXTURES,
    block_facts,
    log_text,
    parse_text,
    refused,
)


@pytest.mark.parametrize(("version", "name"), CORPUS_CASES)
def test_every_committed_log_yields_its_recorded_facts(
    version: str,
    name: str,
) -> None:
    """Compare each log's banner, format, summary, blocks and checks with the table."""
    expected = EXPECTED[version]["logs"][name]
    if "refused" in expected:
        assert refused(log_text(name, version)) == expected["refused"]
        return
    parsed = parse_text(log_text(name, version))
    where = f"{version}/{name}.log"
    assert parsed.banner_version == expected["banner"], f"banner of {where}"
    assert parsed.format == expected["format"], f"format of {where}"
    assert parsed.unsupported_constructs == tuple(expected["unsupported"]), (
        f"unsupported constructs of {where}"
    )
    if expected["manual"] is None:
        assert parsed.manual is None, f"unexpected manual summary in {where}"
    else:
        assert parsed.manual is not None, f"missing manual summary in {where}"
        assert [
            parsed.manual.verified,
            parsed.manual.failures,
            parsed.manual.total,
            list(parsed.manual.failed),
        ] == [
            expected["manual"]["verified"],
            expected["manual"]["failures"],
            expected["manual"]["total"],
            expected["manual"]["failed"],
        ], f"manual summary of {where}"
    assert [block_facts(block) for block in parsed.blocks] == expected["blocks"], (
        f"blocks of {where}"
    )


def test_every_supported_version_has_a_corpus() -> None:
    """Require exactly one corpus directory per supported release.

    Comparing the directory names with the supported list catches both a
    missing release and a stale directory left behind after a bump, which a
    comparison against a list built the same way could never show.
    """
    on_disk = {item.name for item in FIXTURES.iterdir() if item.is_dir()}
    assert on_disk == set(SUPPORTED_KANI_VERSIONS), (
        "corpus directories must match the supported releases"
    )
    for version, entries in EXPECTED.items():
        banners = {
            entry["banner"] for entry in entries["logs"].values() if "banner" in entry
        }
        assert banners == {version}, f"every {version} log must carry its own banner"


@pytest.mark.parametrize("version", SUPPORTED_KANI_VERSIONS)
def test_every_recorded_log_exists(version: str) -> None:
    """Require the recorded logs and the files on disk to be the same set.

    Comparing in both directions catches a log added without an expectation as
    well as an expectation left behind by a deleted log; either would otherwise
    be invisible to the parametrized corpus tests.
    """
    recorded = set(EXPECTED[version]["logs"])
    on_disk = {
        path.stem for path in (FIXTURES / version).iterdir() if path.suffix == ".log"
    }
    assert on_disk == recorded, (
        f"{version} logs on disk {sorted(on_disk)} do not match the recorded "
        f"set {sorted(recorded)}"
    )


def test_the_inventory_log_carries_no_verdicts() -> None:
    """Read ``kani list`` output as an inventory rather than a run."""
    parsed = parse_text(log_text("list"))
    assert (parsed.format, parsed.blocks) == ("inventory", ())
    assert parsed.unsupported_constructs == ("TerminatorKind::InlineAsm",)


def test_a_failure_records_where_it_happened() -> None:
    """Keep the source location of each failure, a fact a record cannot invent."""
    block = parse_text(log_text("counterexample")).blocks[0]
    assert [(item.path, item.line) for item in block.failures] == [
        ("src/harnesses.rs", 18),
    ]


def test_the_unwinding_warning_is_recorded_on_the_block_it_names() -> None:
    """Require Kani's unwinding note to survive alongside the failed check."""
    parsed = parse_text(log_text("insufficient_unwind"))
    assert [item.unwinding_reported for item in parsed.blocks] == [True]
    assert [
        item.kind for item in parsed.blocks[0].checks if item.status == "FAILURE"
    ] == ["unwind"]


def test_an_unwinding_note_without_a_failed_unwinding_check_is_refused() -> None:
    """Refuse a note whose stated failure the block never lists.

    The note says the output shows unwinding failures, so a block that lists
    none contradicts itself even when its verdict is successful.
    """
    text = log_text("verified").replace(
        "Verification Time:",
        "[Kani] info: Verification output shows one or more unwinding failures.\n"
        "Verification Time:",
    )

    assert (
        "reports unwinding failures but no failed check describes a bound"
        in refused(
            text,
        )
    )


def test_an_unwinding_note_follows_the_failed_description() -> None:
    """Accept the note when a failed description carries the renderer's phrase.

    Kani 0.67.0 decides the note from the failed properties' descriptions, not
    from their identifiers, so an assertion-shaped identifier must not make the
    genuine capture contradictory.  The identifier still leaves the failure a
    counterexample rather than a resource exhaustion.
    """
    text = (
        log_text("counterexample")
        .replace(
            "This is a placeholder message; Kani doesn't support message "
            "formatted at runtime",
            "unwinding assertion loop 9",
        )
        .replace(
            "Verification Time:",
            "[Kani] info: Verification output shows one or more unwinding "
            "failures.\nVerification Time:",
        )
    )
    block = parse_text(text).blocks[0]
    failed = [item for item in block.properties if item.status == "FAILURE"]

    assert [item.is_unwinding for item in failed] == [False], (
        "the property identifier, not the note, classifies the failure"
    )


def test_a_wrong_version_names_the_observed_and_supported_versions() -> None:
    """A log from another release is refused with both versions in the message."""
    selected = CURRENT_CORPUS.name
    unsupported = "0.0.1" if selected != "0.0.1" else "0.0.2"
    text = log_text("verified").replace(selected, unsupported, 1)
    reason = refused(text)
    assert unsupported in reason
    assert selected in reason
