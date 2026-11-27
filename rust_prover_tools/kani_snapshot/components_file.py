"""The one bounded read of a rustup component record.

`toolchain_model` holds the pure rules: the byte ceiling and the canonical
digest. This module is the adapter that reaches the filesystem, so the model
stays free of I/O and its rules stay testable from literal bytes.

Both callers use `components_digest_of_file`: the producer's build measurement
(`orchestrator.measure_build`) and the consumer's pre-execution check
(`toolchain_binding`). Keeping the read here means the byte ceiling, the
canonicalization and the failure text have one definition.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.toolchain_model import (
    MAX_COMPONENTS_BYTES,
    components_digest,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("components_digest_of_file",)


def components_digest_of_file(path: Path) -> str:
    """Read one rustup component record and return its canonical digest.

    The read is bounded: at most `MAX_COMPONENTS_BYTES + 1` bytes are loaded,
    so the extra byte makes an over-limit file visible without reading it
    whole. `components_digest` then refuses anything over the ceiling.

    Parameters
    ----------
    path : Path
        The toolchain's `lib/rustlib/components` file.

    Returns
    -------
    str
        Lowercase SHA-256 of the canonical sorted component names.

    Raises
    ------
    ProverToolError
        Raised when the file cannot be read, or when its contents are not a
        valid component list.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_COMPONENTS_BYTES + 1)
    except OSError as exc:
        msg = f"failed to read rustup components list: {exc}"
        raise ProverToolError(msg) from exc
    return components_digest(data)
