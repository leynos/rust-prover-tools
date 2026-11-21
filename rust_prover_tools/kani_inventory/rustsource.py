"""Follow Rust module declarations and collect every reachable Kani proof.

The scanner starts at a crate root and follows ``mod`` declarations the way
Rust 2018 resolves them: a module declared in ``lib.rs``, ``main.rs``, or a
``mod.rs`` file looks beside that file; a module declared in ``foo.rs`` looks
under ``foo/``. ``#[path = "..."]`` overrides the location and is the reason
the source-plan use case works when a proof module is registered rather than
placed in the conventional directory.

Discovery refuses a missing module file, an ambiguous pair of candidates
(``foo.rs`` and ``foo/mod.rs``), and a file reachable twice. Those are not
partial-discovery conditions: each makes the resolved inventory describe a tree
Rust itself would reject, so continuing would produce selectors that cannot
exist.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_inventory.items import (
    InlineModItem,
    Item,
    ModItem,
    ProofItem,
    Source,
    parse_level,
)
from rust_prover_tools.kani_inventory.masking import mask_source
from rust_prover_tools.kani_inventory.models import InventoryDiscoveryError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("SourceProof", "collect_proofs")


@dc.dataclass(frozen=True, slots=True)
class SourceProof:
    """One proof found in a source tree.

    Attributes
    ----------
    module_path : tuple[str, ...]
        Module segments relative to the crate root.
    function : str
        Rust function name.
    line : int
        One-based line of the function.
    attributes : tuple[str, ...]
        Proof attributes that selected the item.
    conditional : bool
        Whether an enclosing ``cfg`` other than ``cfg(kani)`` gates the proof.
    source : str
        Repository-relative source file declaring the proof.
    """

    module_path: tuple[str, ...]
    function: str
    line: int
    attributes: tuple[str, ...]
    conditional: bool
    source: str

    @property
    def pretty_name(self) -> str:
        """The fully qualified Kani selector for this proof."""
        return "::".join((*self.module_path, self.function))


@dc.dataclass(frozen=True, slots=True)
class _Module:
    """One file or inline module being walked."""

    path: Path
    module_path: tuple[str, ...]
    conditional: bool
    is_crate_root: bool
    source_relative: str
    path_root: Path


@dc.dataclass(slots=True)
class _Walk:
    """The accumulating state of one crate-root walk."""

    repository_root: Path
    visited: set[Path] = dc.field(default_factory=set)
    proofs: list[SourceProof] = dc.field(default_factory=list)


def collect_proofs(
    root_file: Path,
    repository_root: Path,
) -> tuple[SourceProof, ...]:
    """Collect every proof reachable from one crate root.

    Parameters
    ----------
    root_file : Path
        Crate root source file, such as ``src/lib.rs``.
    repository_root : Path
        Repository root used to make sources relative in the inventory.

    Returns
    -------
    tuple[SourceProof, ...]
        Proofs in source order, with their module paths.

    Raises
    ------
    InventoryDiscoveryError
        Raised for an unreadable file, a missing or ambiguous module file, or
        a module file reachable twice.
    """
    walk = _Walk(repository_root)
    root = root_file.resolve()
    _walk_file(
        walk,
        _Module(
            path=root,
            module_path=(),
            conditional=False,
            is_crate_root=True,
            source_relative=_relative(root, repository_root),
            path_root=_path_root(root, is_crate_root=True),
        ),
    )
    return tuple(walk.proofs)


def _path_root(source: Path, *, is_crate_root: bool) -> Path:
    """Return the directory a ``#[path]`` attribute resolves against."""
    if is_crate_root or source.name == "mod.rs":
        return source.parent
    return source.parent / source.stem


def _relative(path: Path, repository_root: Path) -> str:
    """Return `path` relative to the repository root, or its absolute text."""
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return path.as_posix()


def _walk_file(walk: _Walk, module: _Module) -> None:
    """Walk one module file, following its declarations."""
    resolved = module.path.resolve()
    if resolved in walk.visited:
        msg = f"module file is reachable twice or forms a cycle: {resolved}"
        raise InventoryDiscoveryError(msg)
    walk.visited.add(resolved)
    try:
        original = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        msg = f"module file cannot be read: {resolved}: {error}"
        raise InventoryDiscoveryError(msg) from error
    source = Source(original=original, masked=mask_source(original))
    items = parse_level(
        source,
        0,
        len(original),
        conditional=module.conditional,
    )
    _walk_items(walk, module, items)


def _walk_items(walk: _Walk, module: _Module, items: cabc.Iterable[Item]) -> None:
    """Record proofs and follow module declarations in one file."""
    for item in items:
        if isinstance(item, ProofItem):
            walk.proofs.append(
                SourceProof(
                    module_path=module.module_path,
                    function=item.function,
                    line=item.line,
                    attributes=item.attributes,
                    conditional=item.conditional,
                    source=module.source_relative,
                )
            )
        elif isinstance(item, InlineModItem):
            _walk_items(
                walk,
                dc.replace(
                    module,
                    module_path=(*module.module_path, item.name),
                    conditional=item.conditional,
                    is_crate_root=False,
                    path_root=module.path_root / item.name,
                ),
                item.items,
            )
        else:
            _walk_file(walk, _child_module(walk, module, item))


def _child_module(walk: _Walk, parent: _Module, item: ModItem) -> _Module:
    """Resolve one ``mod`` declaration to the module it introduces."""
    child = _module_file(parent, item)
    return _Module(
        path=child,
        module_path=(*parent.module_path, item.name),
        conditional=item.conditional,
        is_crate_root=False,
        source_relative=_relative(child, walk.repository_root),
        path_root=_path_root(child, is_crate_root=False),
    )


def _module_file(parent: _Module, item: ModItem) -> Path:
    """Resolve one ``mod`` declaration to its file, or refuse an ambiguity."""
    if item.path_attribute is not None:
        candidate = (parent.path_root / item.path_attribute).resolve()
        if not candidate.is_file():
            msg = (
                f"the #[path] module {item.path_attribute!r} declared at "
                f"{parent.path}:{item.line} does not exist"
            )
            raise InventoryDiscoveryError(msg)
        return candidate
    plain = parent.path_root / f"{item.name}.rs"
    nested = parent.path_root / item.name / "mod.rs"
    plain_exists, nested_exists = plain.is_file(), nested.is_file()
    if plain_exists and nested_exists:
        msg = (
            f"module {item.name!r} declared at {parent.path}:{item.line} is "
            f"ambiguous: both {plain} and {nested} exist"
        )
        raise InventoryDiscoveryError(msg)
    if plain_exists:
        return plain
    if nested_exists:
        return nested
    msg = f"module {item.name!r} declared at {parent.path}:{item.line} has no file"
    raise InventoryDiscoveryError(msg)
