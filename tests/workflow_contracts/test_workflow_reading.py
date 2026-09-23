"""Reader cases: the parser and trigger reader refuse what they cannot read.

The trigger cases parse with a resolving loader on purpose. Under it an
unquoted `on:` becomes the boolean `True`, and only a constructed
document can show whether the reader looks under both keys.
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml

from .expressions import ConditionError, conjuncts
from .loading import WorkflowReadingError, load_workflow, read_workflows
from .reading import trigger_declaration, triggers

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_duplicate_key_is_refused() -> None:
    """A job declaring `runs-on` twice cannot hide the first value."""
    text = (
        "on: push\njobs:\n  a:\n    runs-on: paid-label\n    runs-on: ubuntu-latest\n"
    )
    with pytest.raises(WorkflowReadingError, match="duplicate key 'runs-on'"):
        load_workflow(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on: push\n", {"push"}),
        ("on: [push, pull_request]\n", {"push", "pull_request"}),
        ("on:\n  push:\n  pull_request_target:\n", {"push", "pull_request_target"}),
        ("'on':\n  pull_request:\n", {"pull_request"}),
    ],
)
def test_every_trigger_form_is_read(text: str, expected: set[str]) -> None:
    """Scalar, sequence and mapping forms, under either key, are read."""
    read = triggers(yaml.safe_load(text))
    assert read == expected, read


def test_a_workflow_declaring_both_trigger_keys_is_refused() -> None:
    """GitHub merges `on` and `true`; a reader choosing one is blind to the other."""
    document = yaml.safe_load("on: push\n'on': pull_request\n")
    with pytest.raises(WorkflowReadingError, match="exactly once"):
        trigger_declaration(document)


@pytest.mark.parametrize("text", ["jobs: {}\n", "on: 3\n", "on: [push, {a: b}]\n"])
def test_an_unreadable_trigger_declaration_is_refused(text: str) -> None:
    """A missing or unrecognised trigger shape raises rather than reading empty."""
    with pytest.raises(WorkflowReadingError):
        triggers(yaml.safe_load(text))


def test_an_empty_workflow_directory_is_refused(tmp_path: Path) -> None:
    """Finding no workflow is the reader failing, not the repository complying."""
    with pytest.raises(WorkflowReadingError, match="no workflow"):
        read_workflows(tmp_path)


def test_a_missing_workflow_directory_is_named(tmp_path: Path) -> None:
    """A directory that cannot be listed is refused with its name."""
    missing = tmp_path / "absent"
    with pytest.raises(WorkflowReadingError, match="absent could not be listed"):
        read_workflows(missing)


def test_an_unreadable_workflow_is_named(tmp_path: Path) -> None:
    """A workflow that cannot be read is refused with its name, not a bare OSError."""
    (tmp_path / "ok.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (tmp_path / "unreadable.yml").mkdir()
    with pytest.raises(
        WorkflowReadingError, match=r"unreadable\.yml could not be read"
    ):
        read_workflows(tmp_path)


def test_an_uppercase_suffix_is_read(tmp_path: Path) -> None:
    """GitHub runs `.YML` and `.yaml` files, so both are read."""
    (tmp_path / "a.YML").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    read = sorted(read_workflows(tmp_path))
    assert read == ["a.YML", "b.yaml"], read


def test_an_unparseable_workflow_names_its_file(tmp_path: Path) -> None:
    """Invalid YAML is refused with the file named, not a bare parser error."""
    (tmp_path / "broken.yml").write_text("on: [push\n", encoding="utf-8")
    with pytest.raises(WorkflowReadingError, match=r"broken\.yml"):
        read_workflows(tmp_path)


@pytest.mark.parametrize(
    "condition",
    ["a || b", "${{ a && b || c }}", "a &&  b ||c"],
)
def test_an_unquoted_disjunction_is_refused(condition: str) -> None:
    """Any unquoted `||` makes every term optional, so it is refused."""
    with pytest.raises(ConditionError):
        conjuncts(condition)


@pytest.mark.parametrize("condition", ["(a && b", "a) && (b", "a == 'x && b"])
def test_an_unbalanced_condition_is_refused(condition: str) -> None:
    """An unclosed group or literal cannot be split into trustworthy terms."""
    with pytest.raises(ConditionError, match="unbalanced"):
        conjuncts(condition)


def test_a_group_is_one_term() -> None:
    """Operators inside parentheses neither split nor refuse the condition."""
    terms = conjuncts("!(a && b) && (c || d)")
    assert terms == ["!(a && b)", "(c || d)"], terms


def test_a_quoted_operator_is_part_of_its_term() -> None:
    """Operators inside a quoted literal neither split nor refuse."""
    terms = conjuncts("a == 'x || y && z' && b")
    assert terms == ["a == 'x || y && z'", "b"], terms
