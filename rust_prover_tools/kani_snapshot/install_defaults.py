"""Production setup and probe operations for snapshot installation.

Setup runs the snapshot's own launcher, never a host `cargo kani`, with a
generation-private environment. The probe re-executes the installed compiler so
a receipt cannot merely describe paths that no longer hold the pinned binaries.
"""

from __future__ import annotations

import os
import time
import typing as typ

from cuprum.context import UNSET

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_exec_routing import admission_chain, runtime_search_path
from rust_prover_tools.kani_snapshot.identity import execution_environment
from rust_prover_tools.kani_snapshot.receipt_model import (
    ObservedToolchain,
    SetupEvidence,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import toolchain_root

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime

__all__ = ("probe_generation", "run_local_bundle_setup")

#: Setup must not let an inherited `TAR_OPTIONS` change extraction.
_INHERITED_EXTRACTION_VARIABLES: typ.Final[tuple[str, ...]] = ("TAR_OPTIONS",)


def run_local_bundle_setup(
    runtime: SnapshotRuntime,
    bundle: Path,
    timeout_seconds: float | None = None,
) -> None:
    """Run the packaged launcher's setup against the verified bundle.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation whose launcher and private homes setup must use.
    bundle : Path
        Verified backend archive inside the identity's downloads directory.
    timeout_seconds : float | None
        Remaining installation budget. `None` leaves setup unbounded; a
        bounded setup is terminated with its whole process group on expiry.

    Raises
    ------
    CommandFailedError
        Raised when setup exits non-zero.
    ProverToolError
        Raised when setup exceeds the remaining budget.
    """
    launcher = runtime.layout.launcher_dir / "bin" / "cargo-kani"
    env = execution_environment(runtime, os.environ)
    for name in _INHERITED_EXTRACTION_VARIABLES:
        # The environment is an overlay, so removal must be spelled as a
        # Cuprum `UNSET` marker rather than by deleting a copied key.
        env[name] = UNSET
    run_checked(
        CommandSpec(
            (
                str(launcher),
                "setup",
                "--use-local-bundle",
                str(bundle),
            ),
            env=env,
            timeout_seconds=timeout_seconds,
        )
    )


def probe_generation(
    runtime: SnapshotRuntime,
    timeout_seconds: float | None = None,
) -> SetupEvidence:
    """Observe the installed compiler identities and toolchain link.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation to inspect.
    timeout_seconds : float | None
        Remaining installation budget shared by both identity commands, so
        the second only receives what the first left behind. `None` leaves
        the probes unbounded.

    Returns
    -------
    SetupEvidence
        Exact identities and the absolute toolchain link target.

    Raises
    ------
    ProverToolError
        Raised when the link is missing, relative or outside the private
        toolchains, or when an executable cannot report its identity within
        the remaining budget.
    """
    link = runtime.kani_dir / "toolchain"
    try:
        target = link.readlink()
    except OSError as exc:
        msg = f"snapshot generation has no readable toolchain link: {exc}"
        raise ProverToolError(msg) from exc
    if not target.is_absolute():
        msg = f"snapshot toolchain link is not absolute: {target}"
        raise ProverToolError(msg)
    expected = runtime.layout.rustup_home / "toolchains"
    if not target.is_relative_to(expected):
        msg = f"snapshot toolchain link escapes the private toolchains: {target}"
        raise ProverToolError(msg)
    env = execution_environment(runtime, os.environ)
    # The probe must load the same libraries the proof will load. An inherited
    # `LD_LIBRARY_PATH` could otherwise shadow the verified toolchain, so the
    # pinned directory goes first and other Rustup toolchain libraries are
    # dropped, exactly as `proof_environment` does for the proof itself.
    env["LD_LIBRARY_PATH"] = runtime_search_path(
        toolchain_root(runtime) / "lib",
        os.environ.get("LD_LIBRARY_PATH", ""),
    )
    # The probe must exercise the same compiler route the proof will use, so a
    # configured admission wrapper runs outermost and an unusable selection
    # fails here instead of being silently bypassed.
    chain = admission_chain(dict(os.environ))
    started = time.monotonic()

    def remaining_budget() -> float | None:
        """Return what is left of the shared probe budget."""
        if timeout_seconds is None:
            return None
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            msg = "snapshot identity probe exceeded the installation budget"
            raise ProverToolError(msg)
        return remaining

    rustc = run_checked(
        CommandSpec(
            (*chain, str(runtime.rustc), "-vV"),
            env=env,
            timeout_seconds=remaining_budget(),
        )
    )
    kani_compiler = run_checked(
        CommandSpec(
            (*chain, str(runtime.compiler), "--version"),
            env=env,
            timeout_seconds=remaining_budget(),
        )
    )
    return SetupEvidence(
        observed=ObservedToolchain(
            rustc_vv=rustc.stdout.strip(),
            kani_compiler=kani_compiler.stdout.strip(),
        ),
        toolchain_link_target=target,
    )
