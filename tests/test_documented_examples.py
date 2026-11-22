"""Documented examples are read from the shipped text, never from a copy.

The smoke dispatch cannot itself run in a test: it submits a hosted workflow
run that builds on a runner and creates an attestation, which is why the target
is opt-in. What a test can hold is everything up to that boundary — that the
published guide carries the example, and that the command it shows is the one
the Makefile accepts. These witnesses load `docs/users-guide.md` itself, so an
edit to either the example or the recipe breaks the contract they share.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.documentation_examples import (
    DocumentedExample,
    DocumentedExampleError,
    load_examples,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_USERS_GUIDE = _REPO_ROOT / "docs" / "users-guide.md"
_MIGRATION_GUIDE = _REPO_ROOT / "docs" / "rust-prover-tools-v0-2-0-migration-guide.md"
_MAKEFILE = _REPO_ROOT / "Makefile"

#: The identifier the guide gives its smoke-dispatch example.
_SMOKE_EXAMPLE = "kani-snapshot-smoke"

#: The identifier the guide gives its consumer verification example.
_VERIFY_EXAMPLE = "kani-snapshot-verify"

#: The identifier the migration guide gives its snapshot consumer example.
_SNAPSHOT_EXAMPLE = "migration-snapshot-consumer"

#: The documents whose examples this project registers and tests.
_DOCUMENTS = (_USERS_GUIDE, _MIGRATION_GUIDE)


def _examples(document: pathlib.Path) -> dict[str, DocumentedExample]:
    """Return every registered example in one shipped document."""
    return load_examples(document.read_text(encoding="utf-8"), source=str(document))


def _target_recipe(target: str) -> str:
    """Return the recipe lines of one Makefile target."""
    lines = _MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(f"{target}:")
    )
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return "\n".join(recipe)


def test_the_loader_returns_the_fence_under_its_marker() -> None:
    """A marked fence is returned by identifier, with its language and body."""
    text = "\n".join([
        "# Guide",
        "",
        "<!-- tested-example: run-it -->",
        "```sh",
        "make run",
        "```",
    ])

    examples = load_examples(text)

    assert set(examples) == {"run-it"}, "the marked example was not registered"
    example = examples["run-it"]
    assert example.language == "sh", "the fence's language tag was lost"
    assert example.body == "make run", "the fence's body was not extracted"


def test_the_loader_refuses_an_unmarked_fence() -> None:
    """A fence no marker introduces is documentation no test can hold.

    Skipping it would let an example be added, edited or broken without any
    test noticing, which is the drift the convention exists to prevent.
    """
    text = "\n".join(["# Guide", "", "```sh", "make run", "```"])

    with pytest.raises(DocumentedExampleError, match="has no"):
        load_examples(text, source="guide.md")


def test_the_loader_refuses_an_unmarked_tilde_fence() -> None:
    """A `~~~` block is a fence too, so it needs a marker like any other.

    Markdown accepts either fence character, so a loader that recognised only
    backticks would leave the tilde form unregistered: documentation no test
    could ever hold, which is what the marker convention exists to prevent.
    """
    text = "\n".join(["# Guide", "", "~~~sh", "make run", "~~~"])

    with pytest.raises(DocumentedExampleError, match="has no"):
        load_examples(text, source="guide.md")


def test_the_loader_reads_a_marked_tilde_fence() -> None:
    """A marked `~~~` example is registered with its body and language."""
    text = "\n".join([
        "<!-- tested-example: run-it -->",
        "~~~~sh",
        "make run",
        "~~~~",
    ])

    examples = load_examples(text)

    assert set(examples) == {"run-it"}, "the marked tilde example was not registered"
    assert examples["run-it"].language == "sh"
    assert examples["run-it"].body == "make run"


def test_a_backtick_fence_does_not_close_a_tilde_fence() -> None:
    """Closing fences repeat the opening character, so the body stays open."""
    text = "\n".join([
        "<!-- tested-example: run-it -->",
        "~~~sh",
        "make run",
        "```",
    ])

    with pytest.raises(DocumentedExampleError, match="has no closing fence"):
        load_examples(text, source="guide.md")


def test_the_loader_refuses_a_marker_without_an_identifier() -> None:
    """A marker with no identifier registers nothing, so it is refused."""
    text = "\n".join(["<!-- tested-example: -->", "```sh", "make run", "```"])

    with pytest.raises(DocumentedExampleError, match="carries no identifier"):
        load_examples(text, source="guide.md")


def test_the_loader_refuses_a_marker_without_a_fence() -> None:
    """A marker that names no fence is a broken document, not an empty example."""
    text = "\n".join(["<!-- tested-example: run-it -->", "", "```sh", "```"])

    with pytest.raises(DocumentedExampleError, match="not immediately followed"):
        load_examples(text, source="guide.md")


def test_the_loader_refuses_an_unterminated_fence() -> None:
    """An example the reader cannot see the end of is refused."""
    text = "\n".join(["<!-- tested-example: run-it -->", "```sh", "make run"])

    with pytest.raises(DocumentedExampleError, match="has no closing fence"):
        load_examples(text, source="guide.md")


def test_the_loader_refuses_a_duplicate_identifier() -> None:
    """Two examples cannot share the identifier a test would look up."""
    text = "\n".join([
        "<!-- tested-example: run-it -->",
        "```sh",
        "make run",
        "```",
        "<!-- tested-example: run-it -->",
        "```sh",
        "make test",
        "```",
    ])

    with pytest.raises(DocumentedExampleError, match="is used twice"):
        load_examples(text, source="guide.md")


@pytest.mark.parametrize("document", _DOCUMENTS, ids=lambda path: path.name)
def test_every_fence_in_a_shipped_guide_is_registered(
    document: pathlib.Path,
) -> None:
    """Every fence carries a marker, so no example escapes the contract.

    The loader raises on the first unmarked fence. Loading each document in
    full is therefore a check on the document, not only on the examples a test
    happens to look up.
    """
    examples = _examples(document)

    assert examples, "the document registered no examples at all"


def test_the_smoke_example_is_registered_in_the_published_guide() -> None:
    """The shipped guide carries the example under its stable identifier."""
    assert _SMOKE_EXAMPLE in _examples(_USERS_GUIDE), (
        "the smoke dispatch example is not registered"
    )


def test_the_smoke_example_names_the_target_and_its_inputs() -> None:
    """The documented command is the Makefile target with both inputs."""
    example = _examples(_USERS_GUIDE)[_SMOKE_EXAMPLE]

    assert example.language == "sh", "the smoke examples must be shell"
    assert "make kani-snapshot-smoke" in example.body, (
        "the example does not invoke the documented target"
    )
    assert "KANI_SNAPSHOT_UPSTREAM_COMMIT=" in example.body, (
        "the example omits the upstream commit the target requires"
    )
    assert "KANI_SNAPSHOT_PUBLICATION_ID=" in example.body, (
        "the example omits the publication identity the target requires"
    )


def test_the_verify_example_names_the_target_and_its_inputs() -> None:
    """The documented consumer verification runs the shipped target.

    The example cannot execute in an ordinary gate: it installs a pinned
    toolchain and runs real proofs. The test therefore holds the part a
    reader depends on, that the guide and the Makefile agree on the target
    and every input it refuses to run without.
    """
    example = _examples(_USERS_GUIDE)[_VERIFY_EXAMPLE]

    assert example.language == "sh", "the verification example must be shell"
    assert "make kani-snapshot-verify" in example.body, (
        "the example does not invoke the verification target"
    )
    for variable in (
        "KANI_SNAPSHOT_ASSET_DIR=",
        "KANI_SNAPSHOT_WHEEL=",
        "KANI_SNAPSHOT_RUSTC_1_93=",
    ):
        assert variable in example.body, f"the example omits {variable}"
    assert _target_recipe("kani-snapshot-verify"), (
        "the Makefile has no verification target"
    )


def test_the_migration_guide_registers_the_snapshot_consumer_commands() -> None:
    """The migration guide's snapshot example is registered and complete.

    A migration guide is the one document a reader follows verbatim, so the
    commands it shows must be the ones the guide describes: install, check and
    run, in that order, with `install` using the default release transport
    rather than the offline `--asset-dir` alternative.
    """
    example = _examples(_MIGRATION_GUIDE)[_SNAPSHOT_EXAMPLE]

    assert example.language == "bash", "the snapshot example must be shell"
    commands = [line for line in example.body.splitlines() if line.strip()]
    assert commands == [
        "prover-tools kani snapshot install",
        "prover-tools kani snapshot check",
        (
            "prover-tools kani run --snapshot-pin tools/kani/snapshot.toml "
            "-- --harness bounded_push"
        ),
    ], "the guide no longer shows the documented install, check and run sequence"
    assert "--asset-dir" not in example.body, (
        "the documented default is the release transport, not a local directory"
    )


def test_the_smoke_target_accepts_what_the_example_shows() -> None:
    """The recipe reads the same two variables under quoted references.

    The variables are exported and then read as shell data, so a value is never
    pasted into the recipe's own source. Publication stays off: the smoke run
    must never create a release.
    """
    recipe = _target_recipe(_SMOKE_EXAMPLE)

    assert '"$$KANI_SNAPSHOT_UPSTREAM_COMMIT"' in recipe, (
        "the upstream commit is not read as a quoted shell variable"
    )
    assert '"$$KANI_SNAPSHOT_PUBLICATION_ID"' in recipe, (
        "the publication identity is not read as a quoted shell variable"
    )
    assert "publish=false" in recipe, "the smoke dispatch must never publish"
