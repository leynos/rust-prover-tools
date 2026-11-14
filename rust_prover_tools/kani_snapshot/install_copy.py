"""Bounded, resumable-free copies of local release files into staging.

The transport stages each release file through an exclusive pending name so a
reader never observes a partial payload. These helpers own that copy rule:
the size envelope a file must satisfy, the chunked transfer that enforces it
while the file is being written, and the rename that publishes the finished
bytes. Size policy for individual roles lives with the transport that knows
which asset it is copying.
"""

from __future__ import annotations

import dataclasses as dc
import os
import stat
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "TransportFile",
    "TransportLimit",
    "copy_local",
    "stream_bounded",
    "transport_file",
)

_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
_READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024


@dc.dataclass(frozen=True, slots=True)
class TransportLimit:
    """The size rule for one local transport file.

    Attributes
    ----------
    max_bytes : int
        Largest size the copy accepts.
    exact_size : int | None
        Size the manifest declares, when it declares one.
    """

    max_bytes: int
    exact_size: int | None = None


@dc.dataclass(frozen=True, slots=True)
class TransportFile:
    """One local transport file: its asset name and size rule."""

    name: str
    limit: TransportLimit


def transport_file(name: str, max_bytes: int) -> TransportFile:
    """Return the envelope rule for one fixed-name transport file."""
    return TransportFile(name=name, limit=TransportLimit(max_bytes))


def copy_local(
    source: Path,
    destination_dir: Path,
    file: TransportFile,
    checkpoint: Progress | None = None,
) -> Path:
    """Copy one local release file into the private downloads directory."""
    origin = source / file.name
    try:
        status = origin.stat()
    except OSError as exc:
        msg = f"failed to read snapshot asset '{file.name}': {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot asset '{file.name}' is not a regular file"
        raise ProverToolError(msg)
    if status.st_size > file.limit.max_bytes:
        msg = f"snapshot asset '{file.name}' exceeds its transport limit"
        raise ProverToolError(msg)
    exact_size = file.limit.exact_size
    if exact_size is not None and status.st_size != exact_size:
        msg = f"snapshot asset '{file.name}' has the wrong size"
        raise ProverToolError(msg)
    target = destination_dir / file.name
    pending = destination_dir / f".{file.name}.{os.getpid()}.pending"
    try:
        with origin.open("rb") as reader, pending.open("xb") as writer:
            written = stream_bounded(reader, writer, file, checkpoint)
            os.fchmod(writer.fileno(), _PRIVATE_FILE_MODE)
            writer.flush()
            os.fsync(writer.fileno())
        pending.replace(target)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to stage snapshot asset '{file.name}': {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        pending.unlink(missing_ok=True)
        raise
    if exact_size is not None and written != exact_size:
        target.unlink(missing_ok=True)
        msg = f"snapshot asset '{file.name}' has the wrong size"
        raise ProverToolError(msg)
    return target


def stream_bounded(
    reader: typ.BinaryIO,
    writer: typ.BinaryIO,
    file: TransportFile,
    checkpoint: Progress | None = None,
) -> int:
    """Copy until EOF, stopping at the transport bound.

    The pre-copy size check is only a fast path: a file that grows while it is
    copied must also stop at the bound instead of filling the cache.
    """
    progress = as_checkpoint(checkpoint)
    written = 0
    while chunk := reader.read(_READ_CHUNK_SIZE):
        progress()
        written += len(chunk)
        if written > file.limit.max_bytes:
            msg = f"snapshot asset '{file.name}' exceeds its transport limit"
            raise ProverToolError(msg)
        writer.write(chunk)
    return written
