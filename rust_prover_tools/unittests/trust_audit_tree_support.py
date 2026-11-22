"""Shared fixtures and projections for the trust-audit test modules."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.trust_audit import audit_tree
from rust_prover_tools.trust_audit.modules import module_references
from rust_prover_tools.trust_audit.syntax import tokenize

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def references(source: str) -> list[tuple[str, str | None, tuple[str, ...]]]:
    """Return each ``mod`` declaration's name, path attribute and inline chain."""
    return [
        (
            module.name,
            module.path,
            tuple(inline.name for inline in module.enclosing),
        )
        for module in module_references(tokenize(source))
    ]


def write(root: Path, files: cabc.Mapping[str, str]) -> None:
    """Write each source beneath ``root``, creating its parent directory."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def symlink(path: Path, target: Path) -> None:
    """Create a symbolic link, skipping the test where the host refuses one.

    An unprivileged Windows runner without Developer Mode raises ``OSError``
    during setup; that is an environment limit, not an audit failure.
    """
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def audited(root: Path, *drivers: str) -> dict[str, list[str]]:
    """Return each audited file's items, keyed by repository-relative path.

    The raw path sequence is checked for duplicates first: a dictionary would
    silently keep the last of two reports for one file.
    """
    files = audit_tree(root, drivers)
    paths = [file.path for file in files]
    if len(paths) != len(set(paths)):
        pytest.fail(f"duplicate audited files: {paths}")
    return {file.path: [item.item for item in file.items] for file in files}
