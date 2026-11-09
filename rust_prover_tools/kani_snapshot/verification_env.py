"""Child environment for a snapshot proof with Cargo routing isolated.

A snapshot pins a toolchain, not the repository's build configuration. The
proof still compiles the repository's crates, so a profile-level codegen
backend, a competing compiler flag or a host runtime library could quietly
replace the reviewed compiler. The environment handed to the launcher
therefore keeps the caller's admission wrappers, refuses unprovable Cargo
routing, pins every accepted profile backend to LLVM in the child, normalizes
inherited compiler flags and selects the pinned toolchain's own libraries.

The Cargo checks are the same tested helpers the qualified release route uses,
so the two execution paths cannot disagree about what a provable route is.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_exec_routing import runtime_search_path
from rust_prover_tools.kani_release_config import (
    backend_overrides,
    check_verification_config,
)
from rust_prover_tools.kani_release_flags import verification_flags
from rust_prover_tools.kani_snapshot.identity import execution_environment
from rust_prover_tools.kani_snapshot.toolchain_binding import toolchain_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.context import UnsetType

    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime

__all__ = ("proof_environment",)


def proof_environment(
    runtime: SnapshotRuntime,
    base: cabc.Mapping[str, str | UnsetType],
    *,
    cwd: Path,
) -> dict[str, str | UnsetType]:
    """Return the child environment for one snapshot proof invocation.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation whose private homes and toolchain the proof must use.
    base : Mapping[str, str | UnsetType]
        Inherited environment, including any admission wrappers.
    cwd : Path
        Proof working directory whose Cargo configuration is discovered.

    Returns
    -------
    dict[str, str | UnsetType]
        Cuprum overlay selecting the generation, the sanitized verification
        flags in both spellings Cargo reads, the child-only backend pins and
        the pinned toolchain's library directory.

    Raises
    ------
    ProverToolError
        Raised when the repository's Cargo configuration or the inherited
        compiler selection cannot be proved safe.
    """
    env = execution_environment(runtime, base)
    text = _text_environment(base)
    # Discovery reads the private CARGO_HOME the child will actually use, so a
    # host Cargo configuration cannot describe a route the proof never takes.
    discovery = {**text, "CARGO_HOME": str(runtime.layout.cargo_home)}
    config = check_verification_config(cwd, discovery)
    # Effective inherited flags, before this module replaces both spellings.
    flags = verification_flags(text)
    env.update(backend_overrides(config))
    env["LD_LIBRARY_PATH"] = runtime_search_path(
        toolchain_root(runtime) / "lib",
        text.get("LD_LIBRARY_PATH", ""),
    )
    # Kani 0.67 synthesizes encoded flags from the plain spelling, so both
    # carry the same sanitized values.
    env["RUSTFLAGS"] = " ".join(flags)
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    return env


def _text_environment(
    base: cabc.Mapping[str, str | UnsetType],
) -> dict[str, str]:
    """Return only the inherited values that are real strings."""
    return {name: value for name, value in base.items() if isinstance(value, str)}
