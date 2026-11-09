"""Production setup and probe operations for snapshot installation.

Setup runs the snapshot's own launcher, never a host `cargo kani`, with a
generation-private environment. The probe re-executes the installed compiler so
a receipt cannot merely describe paths that no longer hold the pinned binaries.
"""

from __future__ import annotations

import os
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import execution_environment
from rust_prover_tools.kani_snapshot.receipt_model import (
    ObservedToolchain,
    SetupEvidence,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime

__all__ = ("probe_generation", "run_local_bundle_setup")

#: Setup must not let an inherited `TAR_OPTIONS` change extraction.
_INHERITED_EXTRACTION_VARIABLES: typ.Final[tuple[str, ...]] = ("TAR_OPTIONS",)


def run_local_bundle_setup(runtime: SnapshotRuntime, bundle: Path) -> None:
    """Run the packaged launcher's setup against the verified bundle.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation whose launcher and private homes setup must use.
    bundle : Path
        Verified backend archive inside the identity's downloads directory.

    Raises
    ------
    CommandFailedError
        Raised when setup exits non-zero.
    """
    launcher = runtime.layout.launcher_dir / "bin" / "cargo-kani"
    env = execution_environment(runtime, os.environ)
    for name in _INHERITED_EXTRACTION_VARIABLES:
        env.pop(name, None)
    run_checked(
        CommandSpec(
            (
                str(launcher),
                "setup",
                "--use-local-bundle",
                str(bundle),
            ),
            env=env,
        )
    )


def probe_generation(runtime: SnapshotRuntime) -> SetupEvidence:
    """Observe the installed compiler identities and toolchain link.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation to inspect.

    Returns
    -------
    SetupEvidence
        Exact identities and the absolute toolchain link target.

    Raises
    ------
    ProverToolError
        Raised when the link is missing, relative or outside the private
        toolchains, or when an executable cannot report its identity.
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
    rustc = run_checked(CommandSpec((str(runtime.rustc), "-vV"), env=env))
    kani_compiler = run_checked(
        CommandSpec((str(runtime.compiler), "--version"), env=env)
    )
    return SetupEvidence(
        observed=ObservedToolchain(
            rustc_vv=rustc.stdout.strip(),
            kani_compiler=kani_compiler.stdout.strip(),
        ),
        toolchain_link_target=target,
    )
