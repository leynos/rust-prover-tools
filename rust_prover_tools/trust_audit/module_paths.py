"""Resolve Rust module declarations to the repository files they name.

`rust_prover_tools.trust_audit.tree` walks a crate from its entry points; this
module answers where one declaration points.  Keeping the path arithmetic here
separates two questions: which file Rust compiles, and which files the audit is
allowed to read.

A ``#[path]`` value is relative to a directory that depends on where it appears.
At file scope it is the declaring file's own directory; inside an inline module
it is that module's directory, except that the first inline ``#[path]`` is
applied before a deferred file-stem component exists.  A bare ``mod x;`` is
searched beneath the directory the declaring file owns.  Nothing here touches
the filesystem: a target that leaves the root is refused from its text alone,
before its existence is asked about.
"""

from __future__ import annotations

import dataclasses as dc
import os
import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.trust_audit.model import InlineModule, ModuleReference

#: Whether the host reading a ``#[path]`` value treats ``\\`` as a separator.
#: rustc resolves the value with the host's path rules, and on POSIX a
#: backslash is an ordinary filename character.
_WINDOWS = os.name == "nt"

#: The suffix a Rust source file carries.
RUST_SUFFIX: typ.Final[str] = ".rs"
# The files Rust searches for a bare `mod x;` beneath the owning directory:
# a file first, then the module directory's own `mod.rs`.
_MODULE_FILE_CANDIDATES: typ.Final[tuple[str, ...]] = (
    "{name}{suffix}",
    "{name}/mod{suffix}",
)

__all__ = (
    "RUST_SUFFIX",
    "Module",
    "module_target",
    "parent_directory",
    "physical_relative",
    "resolve_repository_path",
)


@dc.dataclass(frozen=True, slots=True)
class Module:
    """A file the walk has yet to scan and the directory its children live in.

    The owner is not derivable from the file's own name. A file loaded by
    ``#[path]`` owns the directory it sits in, while a file loaded by bare
    ``mod x;`` owns the directory ``x`` beneath its declaring file's owner, so
    the walk carries the answer rather than guessing it again.

    ``directory`` is the physical directory holding the file, which a
    ``#[path]`` attribute inside the file resolves against.  ``procedural_macro``
    is the crate kind of the driver this file was reached from, not of the
    nearest manifest above the file: a ``#[path]`` can pull a file in from
    another directory, and it still compiles as part of the driver's crate.
    """

    relative: str
    owner: str
    directory: str
    procedural_macro: bool
    #: Whether a ``#[macro_use]`` import is in scope for this file.  A file
    #: reached from one that imports macros can invoke them without repeating
    #: the attribute, so the walker carries the fact down the module tree.
    macro_use: bool = False


def resolve_repository_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    """Resolve a repository-relative path, refusing anything outside the root.

    Containment is checked on the resolved path, so ``a/../../b`` cannot borrow
    the root's prefix to escape it.

    Parameters
    ----------
    root : Path
        Repository root.
    relative : str
        Repository-relative path.

    Returns
    -------
    Path
        The resolved absolute path.

    Raises
    ------
    TrustAuditError
        If ``relative`` is absolute or resolves outside ``root``.

    Examples
    --------
    ``src/lib.rs`` resolves beneath the root; ``../elsewhere.rs`` is refused.
    """
    if pathlib.PurePosixPath(relative).is_absolute():
        msg = f"{relative} is absolute; the audit reads only repository files"
        raise TrustAuditError(msg)
    resolved = (root / relative).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        msg = f"{relative} resolves outside the repository"
        raise TrustAuditError(msg)
    return resolved


def module_target(
    root: pathlib.Path,
    module: Module,
    reference: ModuleReference,
) -> Module:
    """Resolve one module declaration, refusing a target outside the root.

    A ``#[path]`` target is relative to the declaring file's directory, as Rust
    resolves it; otherwise the module is searched for beneath its owner. A
    module that resolves to no file is a structural failure: the crate would not
    compile, and the audit would report a clean scan of code it never read.

    Parameters
    ----------
    root : Path
        Repository root; the target must resolve beneath it.
    module : Module
        The file that declares the module.
    reference : ModuleReference
        The declaration's name, path attribute and inline chain.

    Returns
    -------
    Module
        The resolved target, its owner and the directory its own children
        resolve from.

    Raises
    ------
    TrustAuditError
        If the target resolves outside the root, to no file, or to more than
        one file.
    """
    candidates = _module_candidates(module, reference)
    # Containment is checked before existence so an escaping `#[path]` is
    # refused for what it is. Reporting only that the file is absent would
    # invite someone to create it outside the repository and call the audit
    # satisfied.
    if not all(_is_inside(root, candidate) for candidate in candidates):
        msg = (
            f"{module.relative}:{reference.line}:{reference.column} declares "
            f"module {reference.name!r}, which resolves outside the "
            f"repository: {list(candidates)}"
        )
        raise TrustAuditError(msg)
    existing = [candidate for candidate in candidates if (root / candidate).is_file()]
    if not existing:
        msg = (
            f"{module.relative}:{reference.line}:{reference.column} declares "
            f"module {reference.name!r}, which resolves to none of "
            f"{list(candidates)}"
        )
        raise TrustAuditError(msg)
    if len(existing) > 1:
        # rustc rejects a declaration that could name both ``x.rs`` and
        # ``x/mod.rs`` rather than giving either precedence, so the audit must
        # not scan one of them and call the module resolved.
        msg = (
            f"{module.relative}:{reference.line}:{reference.column} declares "
            f"module {reference.name!r}, which is ambiguous between "
            f"{existing}"
        )
        raise TrustAuditError(msg)
    found = existing[0]
    return Module(
        relative=physical_relative(root, found),
        owner=_target_owner(module, reference, found),
        # The compiler derives a child's directory from the pathname it
        # loaded, not from the file a symbolic link reaches, so this stays the
        # declaring pathname's parent.
        directory=parent_directory(found),
        procedural_macro=module.procedural_macro,
        macro_use=module.macro_use,
    )


def physical_relative(root: pathlib.Path, relative: str) -> str:
    """Return the repository-relative path the filesystem actually reaches.

    The candidate is resolved rather than collapsed lexically.  ``src/link``
    may be a symbolic link, and ``src/link/../trusted.rs`` then names a
    different file from ``src/trusted.rs``; the containment and existence
    checks already used the resolved path, so the reported name must be the
    same one.

    Parameters
    ----------
    root : Path
        Repository root that bounds the path.
    relative : str
        Repository-relative path as the compiler spelled it.

    Returns
    -------
    str
        The physical file's repository-relative POSIX path.

    Raises
    ------
    TrustAuditError
        If ``relative`` is absolute or resolves outside ``root``.
    """
    resolved = resolve_repository_path(root, relative)
    return resolved.relative_to(root.resolve()).as_posix()


def _target_owner(
    module: Module,
    reference: ModuleReference,
    found: str,
) -> str:
    """Return the directory the loaded file owns for its own children.

    A ``#[path]``-loaded file owns the directory its pathname sits in.  A bare
    declaration's target owns the directory named after the module, whether it
    was found as ``name.rs`` or ``name/mod.rs``.  Both keep the compiler's
    spelling, including any ``..`` it resolves at lookup time.
    """
    if reference.path is not None:
        return parent_directory(found)
    search = pathlib.PurePosixPath(_search_directory(module, reference))
    return (search / reference.name).as_posix()


def _module_candidates(
    module: Module,
    reference: ModuleReference,
) -> tuple[str, ...]:
    """Return the paths one declaration may name.

    A path attribute names one file.  A bare declaration is searched for
    beneath the declaration's owner, which may be an inline module's directory;
    the first candidate is the file and the second is the module directory's
    own ``mod.rs``.
    """
    if reference.path is not None:
        base = pathlib.PurePosixPath(_path_base(module, reference))
        return ((base / _portable(reference.path)).as_posix(),)
    directory = pathlib.PurePosixPath(_search_directory(module, reference))
    return tuple(
        (
            directory / template.format(name=reference.name, suffix=RUST_SUFFIX)
        ).as_posix()
        for template in _MODULE_FILE_CANDIDATES
    )


def _portable(value: str) -> str:
    """Return a ``#[path]`` value in the separator form the host resolves.

    rustc applies the host's path rules, so a Windows backslash separates
    components there while a POSIX backslash is part of the filename.
    Normalising once, before the owner and directory are derived, keeps a
    child's search directory consistent with the file the lookup found.
    """
    return value.replace("\\", "/") if _WINDOWS else value


def _path_base(module: Module, reference: ModuleReference) -> str:
    """Return the directory a ``#[path]`` value is relative to.

    At file scope the value is relative to the declaring file's own directory —
    ``src/a/b.rs`` writing ``#[path = "foo.rs"]`` means ``src/a/foo.rs`` — so
    the declaring file's owner directory plays no part.

    Inside ``mod inline { … }`` the value is relative to that inline module's
    directory, which is the declaring file's owner extended by each inline name
    or ``#[path]`` value.
    """
    if not reference.enclosing:
        return module.directory
    return _inline_directory(
        module.owner,
        module.directory,
        reference.enclosing,
    )


def _search_directory(module: Module, reference: ModuleReference) -> str:
    """Return the directory a bare ``mod name;`` is searched beneath.

    An inline module is a module in its own right, so its declarations are
    searched for beneath its name — or beneath its own ``#[path]`` value, which
    replaces the directory component entirely.
    """
    if not reference.enclosing:
        return module.owner
    return _inline_directory(
        module.owner,
        module.directory,
        reference.enclosing,
    )


def _inline_directory(
    owner: str,
    directory: str,
    enclosing: cabc.Iterable[InlineModule],
) -> str:
    """Return the directory an inline chain resolves to.

    A bare inline module appends its name to the directory the enclosing file
    owns, which already carries any deferred file-stem component.  A first
    inline module carrying ``#[path]`` is different: rustc resolves the value
    against the file's *physical* directory, before the deferred component
    exists, so ``#[path = "parts"]`` inside ``src/a.rs`` names ``src/parts``
    rather than ``src/a/parts``.  A later ``#[path]`` is relative to the
    inline module that holds it, which is why the running directory is the base
    from then on.
    """
    current = pathlib.PurePosixPath(owner)
    first = True
    for inline in enclosing:
        if inline.path is not None:
            base = pathlib.PurePosixPath(directory) if first else current
            current = base / pathlib.PurePosixPath(inline.path)
        else:
            current /= inline.name
        first = False
    return current.as_posix()


def parent_directory(relative: str) -> str:
    """Return the directory holding a repository-relative file.

    Parameters
    ----------
    relative : str
        Repository-relative path.

    Returns
    -------
    str
        The path's parent in POSIX form; ``.`` for a top-level file.
    """
    return pathlib.PurePosixPath(relative).parent.as_posix()


def _is_inside(root: pathlib.Path, relative: str) -> bool:
    """Report whether a repository-relative path stays beneath ``root``."""
    try:
        resolve_repository_path(root, relative)
    except TrustAuditError:
        return False
    return True
