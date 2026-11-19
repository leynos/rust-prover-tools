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
    DocumentedExampleError,
    load_examples,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_USERS_GUIDE = _REPO_ROOT / "docs" / "users-guide.md"
_MAKEFILE = _REPO_ROOT / "Makefile"

#: The identifier the guide gives its smoke-dispatch example.
_SMOKE_EXAMPLE = "kani-snapshot-smoke"


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


def test_the_smoke_example_is_registered_in_the_published_guide() -> None:
    """The shipped guide carries the example under its stable identifier."""
    examples = load_examples(
        _USERS_GUIDE.read_text(encoding="utf-8"), source=str(_USERS_GUIDE)
    )

    assert _SMOKE_EXAMPLE in examples, "the smoke dispatch example is not registered"


def test_the_smoke_example_names_the_target_and_its_inputs() -> None:
    """The documented command is the Makefile target with both inputs."""
    example = load_examples(
        _USERS_GUIDE.read_text(encoding="utf-8"), source=str(_USERS_GUIDE)
    )[_SMOKE_EXAMPLE]

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
