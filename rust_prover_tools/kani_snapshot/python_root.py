"""Recorded content contract for the setup-created Python root.

The backend inventory cannot describe `pyroot`: setup creates it, so it is an
allowed extra when the installed tree is compared with the inventory. That
allowance would otherwise accept any content setup happened to leave there,
even though execution puts `pyroot` on `PYTHONPATH` and `pyroot/bin` on `PATH`.
Installation therefore records the exact members it produced, and every reuse
requires the same members, so a later extra file cannot silently become
importable or executable.

A record of link text alone would not close the allowance: a link can reach
content the record does not describe. Links below the root must therefore
resolve inside the root itself, so everything reachable through one is part of
the recorded tree rather than, for example, a mutable generation home whose
later changes the record would not cover.

The record is anchored in the generation, like the receipt, not in the
manifest: the producer does not publish the Python tree, so setup is the only
witness to what it created. Re-recording identical bytes on a fresh install
keeps the contract deterministic rather than trusting a timestamp.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import typing as typ
from pathlib import Path

from rust_prover_tools.download_stream import READ_CHUNK_SIZE
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import normalize_member_path
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_SYMLINK,
    MAX_INVENTORY_PATH_LENGTH,
)
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime

__all__ = ("record_python_root", "require_python_root_matches")

_SCHEMA_VERSION: typ.Final[int] = 1
_MEMBER_PREVIEW: typ.Final[int] = 5


def record_python_root(
    runtime: SnapshotRuntime,
    *,
    checkpoint: Progress | None = None,
) -> bytes:
    """Return the canonical record of the current Python root's members.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Runtime whose `pyroot` is described.
    checkpoint : Progress | None
        Called while the tree is walked and member digests are computed.
        `None` is a no-op.

    Returns
    -------
    bytes
        Canonical JSON, deterministic for identical content. An absent root
        records an empty member list rather than failing, because a snapshot
        without a Python payload has nothing to describe.

    Raises
    ------
    ProverToolError
        Raised when the root escapes the generation, one of its links escapes
        the root, a member is a special file, or a member path is not a plain
        relative path.
    """
    progress = as_checkpoint(checkpoint)
    members = _members(runtime, progress)
    document = {"schema_version": _SCHEMA_VERSION, "members": members}
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def require_python_root_matches(
    runtime: SnapshotRuntime,
    *,
    checkpoint: Progress | None = None,
) -> None:
    """Require the Python root to be exactly what installation recorded.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Runtime whose generation holds both the record and the tree.
    checkpoint : Progress | None
        Called while the tree is walked and member digests are computed.

    Raises
    ------
    ProverToolError
        Raised when the record is missing or unreadable, or the root differs
        from it by a missing, extra or changed member.
    """
    record = runtime.layout.python_root_record_path
    try:
        recorded = record.read_bytes()
    except OSError as exc:
        msg = f"snapshot Python root has no recorded setup content: {exc}"
        raise ProverToolError(msg) from exc
    actual = record_python_root(runtime, checkpoint=checkpoint)
    if actual == recorded:
        return
    raise ProverToolError(_mismatch(recorded, actual))


def _members(runtime: SnapshotRuntime, progress: Progress) -> list[dict[str, object]]:
    """Return one record object per member below the Python root."""
    root = runtime.python_root
    try:
        root_status = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        msg = f"failed to inspect snapshot Python root '{root}': {exc}"
        raise ProverToolError(msg) from exc
    _require_real_directory(root, root_status)
    _require_generation_containment(root, runtime)
    members: list[dict[str, object]] = []
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        progress()
        for child in _scandir(directory):
            relative = f"{prefix}/{child.name}" if prefix else child.name
            entry, is_directory = _member(child, relative, runtime, progress)
            members.append(entry)
            if is_directory:
                stack.append((Path(child.path), relative))
    members.sort(key=lambda member: typ.cast("str", member["path"]))
    return members


def _member(
    child: os.DirEntry[str],
    relative: str,
    runtime: SnapshotRuntime,
    progress: Progress,
) -> tuple[dict[str, object], bool]:
    """Return one member's record and whether it is a real directory."""
    try:
        path = normalize_member_path(relative, max_length=MAX_INVENTORY_PATH_LENGTH)
    except ProverToolError as exc:
        msg = f"snapshot Python root member path is invalid: {exc}"
        raise ProverToolError(msg) from exc
    try:
        status = child.stat(follow_symlinks=False)
    except OSError as exc:
        msg = f"failed to inspect snapshot Python root member '{path}': {exc}"
        raise ProverToolError(msg) from exc
    mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISDIR(status.st_mode):
        return (
            {"path": path, "kind": KIND_DIRECTORY, "mode": mode},
            True,
        )
    if stat.S_ISLNK(status.st_mode):
        _require_contained_link(Path(child.path), runtime)
        return (
            {
                "path": path,
                "kind": KIND_SYMLINK,
                "mode": mode,
                "target": _readlink(Path(child.path)),
            },
            False,
        )
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot Python root contains a special file: '{path}'"
        raise ProverToolError(msg)
    return (
        {
            "path": path,
            "kind": KIND_FILE,
            "mode": mode,
            "size": status.st_size,
            "sha256": _sha256_file(Path(child.path), progress),
        },
        False,
    )


def _require_generation_containment(root: Path, runtime: SnapshotRuntime) -> None:
    """Reject a root that resolves outside its own generation."""
    generation = _resolve(runtime.layout.generation_dir, "snapshot generation")
    if not _resolve(root, "snapshot Python root").is_relative_to(generation):
        msg = f"snapshot Python root resolves outside its generation: {root}"
        raise ProverToolError(msg)


def _require_real_directory(root: Path, status: os.stat_result) -> None:
    """Reject a root that is a link or not a directory."""
    if stat.S_ISLNK(status.st_mode):
        msg = f"snapshot Python root must not be a symbolic link: {root}"
        raise ProverToolError(msg)
    if not stat.S_ISDIR(status.st_mode):
        msg = f"snapshot Python root is not a directory: {root}"
        raise ProverToolError(msg)


def _require_contained_link(path: Path, runtime: SnapshotRuntime) -> None:
    """Reject a Python-root link whose resolved target leaves the root.

    The record stores a link's text, not the bytes it reaches, and execution
    puts the root on `PYTHONPATH` and its `bin` on `PATH`. Keeping every target
    inside the recorded tree means a link cannot redirect execution at a
    sibling home whose later changes the record does not observe.
    """
    root = _resolve(runtime.python_root, "snapshot Python root")
    if not _resolve(path, "snapshot Python root link").is_relative_to(root):
        msg = f"snapshot Python root link escapes its root: {path}"
        raise ProverToolError(msg)


def _readlink(path: Path) -> str:
    """Return one link's target text, reporting read failures."""
    try:
        return str(path.readlink())
    except OSError as exc:
        msg = f"failed to read snapshot Python root link '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _scandir(directory: Path) -> list[os.DirEntry[str]]:
    """Return one directory's entries, reporting inspection failures."""
    try:
        with os.scandir(directory) as children:
            return list(children)
    except OSError as exc:
        msg = f"failed to inspect snapshot Python root directory '{directory}': {exc}"
        raise ProverToolError(msg) from exc


def _sha256_file(path: Path, progress: Progress) -> str:
    """Return one file's digest, reporting read failures."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(READ_CHUNK_SIZE):
                progress()
                digest.update(chunk)
    except OSError as exc:
        msg = f"failed to read snapshot Python root member '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return digest.hexdigest()


def _resolve(path: Path, description: str) -> Path:
    """Resolve one path, reporting unreadable or looping links."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        msg = f"failed to resolve {description} '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _mismatch(recorded: bytes, actual: bytes) -> str:
    """Name the first difference between two member records."""
    expected = _describe(recorded)
    observed = _describe(actual)
    if expected is None or observed is None:
        return "snapshot Python root record is not valid JSON"
    missing = sorted(set(expected) - set(observed))
    if missing:
        return f"snapshot Python root is missing recorded members: {_preview(missing)}"
    extra = sorted(set(observed) - set(expected))
    if extra:
        return (
            "snapshot Python root contains members the record does not "
            f"describe: {_preview(extra)}"
        )
    changed = sorted(
        path for path, member in expected.items() if observed.get(path) != member
    )
    return f"snapshot Python root members do not match the record: {_preview(changed)}"


def _describe(data: bytes) -> dict[str, object] | None:
    """Return a member mapping for diagnostics, or `None` when unreadable."""
    try:
        document = json.loads(data)
    except ValueError:
        # `UnicodeDecodeError` is a `ValueError`, so this covers both a bad
        # encoding and malformed JSON without a two-type handler that only
        # Python 3.14 can parse unparenthesised.
        return None
    if not isinstance(document, dict):
        return None
    members = document.get("members")
    if not isinstance(members, list):
        return None
    described: dict[str, object] = {}
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("path"), str):
            return None
        described[member["path"]] = member
    return described


def _preview(paths: list[str]) -> str:
    """Render a bounded, deterministic list of member paths."""
    shown = ", ".join(paths[:_MEMBER_PREVIEW])
    if len(paths) > _MEMBER_PREVIEW:
        return f"{shown}, ... ({len(paths)} total)"
    return shown
