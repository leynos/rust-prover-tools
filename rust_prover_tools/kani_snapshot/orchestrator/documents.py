"""Write a rendered document where the next job can read it.

Rendering belongs to the producer, which decides the exact bytes; writing them
belongs here, with the other effects, so the producer package stays free of a
filesystem it would have to be tested against.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

from rust_prover_tools.errors import ProverToolError

__all__ = ("write_document",)


def write_document(path: pathlib.Path, data: bytes) -> None:
    """Write bytes atomically so a reader never sees a partial document.

    Parameters
    ----------
    path : pathlib.Path
        Destination file. Its parent directories are created.
    data : bytes
        Exact document bytes.

    Raises
    ------
    ProverToolError
        Raised when the temporary file or the replacement cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    os.close(handle)
    temporary = pathlib.Path(name)
    try:
        with temporary.open("wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        msg = f"failed to write '{path}': {exc}"
        raise ProverToolError(msg) from exc
