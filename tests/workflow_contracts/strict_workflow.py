"""Strict readers for workflow contract tests.

A workflow is executable policy, so the tests that pin it must read the same
document GitHub reads. PyYAML's default loader is too forgiving in two ways
that matter here: it silently keeps the last duplicate key, and it reads `on`
as a boolean. Both would let a contract test assert something other than what
GitHub runs, so this module refuses duplicate keys and resolves only `true`
and `false` as booleans.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import yaml
import yaml.constructor
import yaml.resolver

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "KANI_WORKFLOW_PATH",
    "REPO_ROOT",
    "artifact_names",
    "declared_text",
    "dependency_names",
    "job",
    "named_scripts",
    "read_workflow",
    "run_scripts",
    "steps",
    "strict_load",
    "triggers",
    "uses_entries",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KANI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "kani-monthly.yml"

_BOOL_ONLY = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate keys and reads `on` as text."""


# SafeLoader keeps YAML 1.1's `on`/`off`/`yes`/`no` boolean words, and an
# added resolver is consulted after the inherited one. Dropping the inherited
# bool tag first is what makes `on:` a string key, as GitHub reads it.
_StrictLoader.yaml_implicit_resolvers = {
    first: [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_ONLY, list("tTfF"))


def _construct_mapping(
    loader: _StrictLoader, node: yaml.MappingNode
) -> dict[object, object]:
    """Construct one mapping, refusing a key that appears twice."""
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            msg = f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            raise yaml.constructor.ConstructorError(
                None, None, msg, key_node.start_mark
            )
        seen.add(key)
    return typ.cast(
        "dict[object, object]",
        yaml.SafeLoader.construct_mapping(loader, node, deep=True),
    )


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def strict_load(text: str) -> dict[str, object]:
    """Parse one YAML document, refusing duplicate keys."""
    document = yaml.load(text, Loader=_StrictLoader)  # ruff: ignore[unsafe-yaml-load] - safe subclass
    if not isinstance(document, dict):
        msg = "workflow document must be a mapping"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", document)


def read_workflow(path: Path = KANI_WORKFLOW_PATH) -> dict[str, object]:
    """Read and strictly parse a workflow file."""
    return strict_load(path.read_text(encoding="utf-8"))


def triggers(document: dict[str, object]) -> dict[str, object]:
    """Return the workflow's `on` mapping, however it is spelled."""
    found = document.get("on")
    if not isinstance(found, dict):
        msg = "workflow must declare an `on` mapping"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", found)


def job(document: dict[str, object], name: str) -> dict[str, object]:
    """Return one job's declaration."""
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or name not in jobs:
        msg = f"workflow declares no {name!r} job"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", jobs[name])


def steps(declaration: dict[str, object]) -> list[dict[str, object]]:
    """Return a job's steps as mappings."""
    found = declaration.get("steps")
    if not isinstance(found, list):
        msg = "job must declare a steps list"
        raise TypeError(msg)
    return [typ.cast("dict[str, object]", step) for step in found]


def run_scripts(declaration: dict[str, object]) -> list[str]:
    """Return every shell body a job runs."""
    return [
        typ.cast("str", step["run"])
        for step in steps(declaration)
        if isinstance(step.get("run"), str)
    ]


def _step_text(step: dict[str, object]) -> list[str]:
    """Return the shell body and the env and input values one step declares."""
    parts = [
        value for key, value in step.items() if key == "run" and isinstance(value, str)
    ]
    for key in ("env", "with"):
        values = step.get(key)
        if isinstance(values, dict):
            declared = typ.cast("dict[str, object]", values)
            parts += [str(value) for value in declared.values()]
    return parts


def declared_text(declaration: dict[str, object]) -> str:
    """Return every string a job declares, in steps, env and inputs."""
    return "\n".join(part for step in steps(declaration) for part in _step_text(step))


def named_scripts(declaration: dict[str, object]) -> dict[str, str]:
    """Return the shell body of each named step, keyed by step name."""
    return {
        typ.cast("str", step["name"]): typ.cast("str", step["run"])
        for step in steps(declaration)
        if isinstance(step.get("name"), str) and isinstance(step.get("run"), str)
    }


def uses_entries(document: dict[str, object]) -> list[str]:
    """Return every `uses:` reference in the workflow."""
    found: list[str] = []
    for name in typ.cast("dict[str, object]", document["jobs"]):
        for step in steps(job(document, name)):
            reference = step.get("uses")
            if isinstance(reference, str):
                found.append(reference)
    return found


def _artifact_uses(declaration: dict[str, object], action: str) -> list[str]:
    """Return the `name:` of every use of one artefact action in a job."""
    names: list[str] = []
    for step in steps(declaration):
        reference = step.get("uses")
        if not isinstance(reference, str) or not reference.startswith(action):
            continue
        with_ = typ.cast("dict[str, object]", step.get("with", {}))
        names.append(typ.cast("str", with_["name"]))
    return names


def artifact_names(
    document: dict[str, object],
) -> tuple[list[str], list[str]]:
    """Return the names uploaded and downloaded anywhere in the workflow."""
    uploaded: list[str] = []
    downloaded: list[str] = []
    for name in typ.cast("dict[str, object]", document["jobs"]):
        declaration = job(document, name)
        uploaded += _artifact_uses(declaration, "actions/upload-artifact@")
        downloaded += _artifact_uses(declaration, "actions/download-artifact@")
    return uploaded, downloaded


def as_mapping(value: object, what: str) -> dict[str, object]:
    """Return `value` as a mapping or refuse the document."""
    if not isinstance(value, dict):
        msg = f"{what} must be a mapping"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", value)


def dependency_names(declaration: dict[str, object]) -> list[str]:
    """Return a job's `needs:` as a list, however it is spelled."""
    needed = declaration.get("needs")
    if isinstance(needed, str):
        return [needed]
    if isinstance(needed, list):
        return [typ.cast("str", item) for item in needed]
    return []


def as_sequence(value: object, what: str) -> cabc.Sequence[object]:
    """Return `value` as a sequence or refuse the document."""
    if not isinstance(value, list):
        msg = f"{what} must be a list"
        raise TypeError(msg)
    return value
