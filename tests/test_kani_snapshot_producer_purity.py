"""Contract test for the producer package's purity boundary.

The approved plan and D5 keep `rust_prover_tools.kani_snapshot.producer` free
of effects: it decides what may be built, from which upstream identity, and
renders the documents a job hands to the next one, but it never reads or
writes a path, starts a process, reads the environment or touches the network.
Those effects belong to `rust_prover_tools.kani_snapshot.orchestrator`, which
the workflow invokes.

The check reads each module's syntax tree rather than trusting a naming
convention, so a new `import subprocess` or a `Path.write_text` call fails here
even before any test exercises the code path that reaches it.
"""

from __future__ import annotations

import ast
import typing as typ

from tests.workflow_contracts.strict_workflow import (
    REPO_ROOT,
    as_mapping,
    read_workflow,
    run_scripts,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

PRODUCER_DIR = REPO_ROOT / "rust_prover_tools" / "kani_snapshot" / "producer"

#: Modules whose import would give the pure package an effect it may not have.
#: `pathlib` is deliberately absent: a `Path` value is inert, and assembly names
#: its output directories as values before any job writes them.
FORBIDDEN_MODULES: typ.Final[frozenset[str]] = frozenset({
    "argparse",
    "http",
    "httpx",
    "os",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "urllib",
})

#: Call names that touch the filesystem or the environment wherever they appear.
#: The set is deliberately narrow: a name here must be an effect on its own, so
#: a false positive cannot be mistaken for policy. Generic names such as `run`
#: or `replace` are excluded; an effect reached through `os` or `subprocess` is
#: already refused by the import check.
FORBIDDEN_CALLS: typ.Final[frozenset[str]] = frozenset({
    "Popen",
    "check_call",
    "check_output",
    "getenv",
    "hardlink_to",
    "iterdir",
    "mkdir",
    "open",
    "read_bytes",
    "read_text",
    "rmdir",
    "symlink_to",
    "system",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
})


def _producer_modules() -> list[Path]:
    """Return every producer module, in a stable order."""
    return sorted(PRODUCER_DIR.glob("*.py"))


def _module_names(node: ast.AST) -> list[str]:
    """Return the dotted names one import node refers to.

    A `from X import Y` names both `X` and `X.Y`. The imported attribute may
    itself be a module — `from pkg import orchestrator` is a way of reaching
    a submodule without writing its dotted path — so reading only
    `node.module` would let that spelling through the boundary. A relative
    import is recorded by the member name alone, which is all the syntax
    carries.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    source = typ.cast("ast.ImportFrom", node)
    module = source.module or ""
    if not module:
        return [alias.name for alias in source.names]
    return [module, *(f"{module}.{alias.name}" for alias in source.names)]


def _imported_names(tree: ast.Module) -> list[str]:
    """Return the dotted name of every import in one module."""
    return [
        name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in _module_names(node)
    ]


def _called_names(tree: ast.Module) -> list[str]:
    """Return the final name of every call the module makes."""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            names.append(function.id)
        elif isinstance(node.func, ast.Attribute):
            names.append(typ.cast("ast.Attribute", function).attr)
    return names


def test_a_submodule_imported_from_its_package_is_seen() -> None:
    """`from pkg import orchestrator` reaches the same module as a dotted path.

    Reading only `node.module` would record `pkg` and let the member name,
    which is the module actually imported, pass the orchestrator check.
    """
    tree = ast.parse("from rust_prover_tools.kani_snapshot import orchestrator")

    assert "rust_prover_tools.kani_snapshot.orchestrator" in _imported_names(tree)


def test_a_relative_submodule_import_is_seen() -> None:
    """A relative import carries only the member name, which must be recorded."""
    tree = ast.parse("from .. import orchestrator")

    assert "orchestrator" in _imported_names(tree)


def test_the_producer_package_has_modules_to_check() -> None:
    """An empty glob would make every test below vacuously true."""
    assert _producer_modules()


def test_the_producer_imports_no_effectful_module() -> None:
    """A process, network, environment or tempfile import is a boundary break."""
    for path in _producer_modules():
        imported = _imported_names(ast.parse(path.read_text(encoding="utf-8")))
        for name in imported:
            root = name.split(".", 1)[0]
            assert root not in FORBIDDEN_MODULES, f"{path.name} imports {name}"


def test_the_producer_never_imports_the_orchestrator() -> None:
    """Dependencies point from the effect layer into the pure policy layer."""
    for path in _producer_modules():
        imported = _imported_names(ast.parse(path.read_text(encoding="utf-8")))
        assert all("orchestrator" not in name for name in imported), path.name


def test_the_producer_calls_no_effectful_function() -> None:
    """Reading or writing a path is the orchestrator's work, not the policy's."""
    for path in _producer_modules():
        called = _called_names(ast.parse(path.read_text(encoding="utf-8")))
        effects = sorted(FORBIDDEN_CALLS.intersection(called))
        assert not effects, f"{path.name} calls {effects}"


def test_every_workflow_command_runs_the_orchestrator() -> None:
    """The workflow invokes the effect layer, never the pure package directly."""
    jobs = as_mapping(read_workflow()["jobs"], "jobs")
    scripts = "\n".join(
        script for name in jobs for script in run_scripts(as_mapping(jobs[name], name))
    )
    assert "rust_prover_tools.kani_snapshot.producer." not in scripts
    assert "rust_prover_tools.kani_snapshot.orchestrator.entrypoint" in scripts
