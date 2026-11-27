"""Production setup and probe operations for snapshot installation.

Setup runs the snapshot's own launcher, never a host `cargo kani`, with a
generation-private environment. The probe re-executes the installed compiler so
a receipt cannot merely describe paths that no longer hold the pinned binaries.
"""

from __future__ import annotations

import collections.abc as cabc
import os
import time
import typing as typ

from cuprum.context import UNSET, EnvMode, UnsetType

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

__all__ = (
    "AdmissionResolver",
    "disable_rustup_self_update",
    "probe_generation",
    "probe_host_generation",
    "run_local_bundle_setup",
)

#: Resolves the compiler admission wrappers from an injected environment.
type AdmissionResolver = cabc.Callable[[cabc.Mapping[str, str]], tuple[str, ...]]

#: Setup must not let an inherited `TAR_OPTIONS` change extraction.
_INHERITED_EXTRACTION_VARIABLES: typ.Final[tuple[str, ...]] = ("TAR_OPTIONS",)

#: Rustup's config command, run before the launcher starts.
_DISABLE_SELF_UPDATE: typ.Final[tuple[str, ...]] = (
    "rustup",
    "set",
    "auto-self-update",
    "disable",
)


def disable_rustup_self_update(
    env: cabc.Mapping[str, str | UnsetType],
    *,
    timeout_seconds: float | None = None,
) -> None:
    """Turn rustup's automatic self-update off in the generation's own home.

    This writes rustup's `auto-self-update = disable` setting into the private
    rustup home that `env` names. Rustup's self-update expects its own binary
    under `CARGO_HOME`. This generation's cargo home is private and holds no
    rustup, so the update attempt fails after the toolchain has already been
    installed. Disabling it with the same private homes is version-agnostic and
    keeps setup deterministic; an inherited or user-level setting could
    otherwise re-enable it.

    Parameters
    ----------
    env : collections.abc.Mapping
        The setup environment, including the private `RUSTUP_HOME` and
        `CARGO_HOME`.
    timeout_seconds : float | None
        Budget for the configuration command alone.

    Raises
    ------
    CommandFailedError
        Raised when `rustup` itself fails.
    """
    run_checked(
        CommandSpec(
            _DISABLE_SELF_UPDATE,
            env=env,
            timeout_seconds=timeout_seconds,
        )
    )


def run_local_bundle_setup(
    runtime: SnapshotRuntime,
    bundle: Path,
    timeout_seconds: float | None = None,
    *,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> None:
    """Configure the private homes, then run the packaged launcher's setup.

    Two processes run, in order:

    1. `disable_rustup_self_update` writes the `auto-self-update = disable`
       setting into the generation's private rustup home. Without it a consumer
       install fails after the toolchain has been installed.
    2. The packaged `cargo-kani setup --use-local-bundle` installs the verified
       prebuilt bundle.

    Both share one installation budget: the launcher receives only what the
    configuration step left, so a slow `rustup` cannot push setup past the
    caller's deadline.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation whose launcher and private homes setup must use.
    bundle : Path
        Verified backend archive inside the identity's downloads directory.
    timeout_seconds : float | None
        Remaining installation budget, shared by both processes. `None` leaves
        setup unbounded; a bounded setup is terminated with its whole process
        group on expiry.
    clock : collections.abc.Callable
        Reads elapsed time, defaulting to `time.monotonic`. Injected so the
        budget arithmetic is testable without patching the module's clock.

    Raises
    ------
    CommandFailedError
        Raised when either process exits non-zero.
    ProverToolError
        Raised when the shared budget is already spent when the launcher would
        start.
    """
    launcher = runtime.layout.launcher_dir / "bin" / "cargo-kani"
    env = execution_environment(runtime, os.environ)
    for name in _INHERITED_EXTRACTION_VARIABLES:
        # The environment is an overlay, so removal must be spelled as a
        # Cuprum `UNSET` marker rather than by deleting a copied key.
        env[name] = UNSET
    started = clock()
    disable_rustup_self_update(env, timeout_seconds=timeout_seconds)
    run_checked(
        CommandSpec(
            (
                str(launcher),
                "setup",
                "--use-local-bundle",
                str(bundle),
            ),
            env=env,
            timeout_seconds=_remaining_budget(timeout_seconds, started, clock),
        )
    )


def _remaining_budget(
    timeout_seconds: float | None,
    started: float,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> float | None:
    """Return the seconds left of the setup budget, refusing an expired one."""
    if timeout_seconds is None:
        return None
    remaining = timeout_seconds - (clock() - started)
    if remaining <= 0:
        msg = "snapshot setup exceeded the installation budget"
        raise ProverToolError(msg)
    return remaining


def probe_generation(
    runtime: SnapshotRuntime,
    timeout_seconds: float | None = None,
    *,
    environment: cabc.Mapping[str, str],
    resolve_admission: AdmissionResolver = admission_chain,
) -> SetupEvidence:
    """Observe the installed compiler identities and toolchain link.

    This function runs two processes, so it is the effectful half of a
    snapshot check; `rust_prover_tools.kani_snapshot.install_validation.
    validate_generation` remains the process-free query. The environment and
    the admission resolver are injected rather than read from `os.environ`, so
    a caller can prove which environment the probe used. The children receive
    exactly that mapping, resolved through the generation's own homes, rather
    than an overlay that would keep unset host variables alive. Use
    `probe_host_generation` to run against the host's own environment.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Generation to inspect.
    timeout_seconds : float | None
        Remaining installation budget shared by both identity commands, so
        the second only receives what the first left behind. `None` leaves
        the probes unbounded.
    environment : collections.abc.Mapping[str, str]
        Inherited environment the probe derives the child environment and the
        admission route from. `os.environ` is the production value.
    resolve_admission : AdmissionResolver
        Resolves the admission wrappers from `environment`. The default keeps
        the reviewed routing rule: the probe exercises the configured wrapper.

    Returns
    -------
    SetupEvidence
        Exact identities and the absolute toolchain link target.

    Raises
    ------
    ProverToolError
        Raised when the link is missing, relative or outside the private
        toolchains, when the admission route is malformed, or when an
        executable cannot report its identity within the remaining budget.
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
    env = _probe_environment(runtime, environment)
    # The probe must load the same libraries the proof will load. An inherited
    # `LD_LIBRARY_PATH` could otherwise shadow the verified toolchain, so the
    # pinned directory goes first and other Rustup toolchain libraries are
    # dropped, exactly as `proof_environment` does for the proof itself.
    env["LD_LIBRARY_PATH"] = runtime_search_path(
        toolchain_root(runtime) / "lib",
        environment.get("LD_LIBRARY_PATH", ""),
    )
    # The probe must exercise the same compiler route the proof will use, so a
    # configured admission wrapper runs outermost and an unusable selection
    # fails here instead of being silently bypassed.
    chain = resolve_admission(environment)
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
            env_mode=EnvMode.REPLACE,
            timeout_seconds=remaining_budget(),
        )
    )
    kani_compiler = run_checked(
        CommandSpec(
            (*chain, str(runtime.compiler), "--version"),
            env=env,
            env_mode=EnvMode.REPLACE,
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


def _probe_environment(
    runtime: SnapshotRuntime,
    environment: cabc.Mapping[str, str],
) -> dict[str, str]:
    """Return the probe's child environment, derived only from `environment`.

    `execution_environment` returns an overlay, and an overlay leaves every
    variable it does not name at the value the live process holds. The probe
    must run in the environment its caller supplied, so the overlay is resolved
    here into a replacement mapping: the generation's own homes and toolchain
    are set, the selectors it must not inherit are dropped, and every other
    variable is the caller's value. A caller that passes `os.environ` therefore
    gets the host environment, and one that passes a narrow mapping does not
    silently inherit the rest of the host's.
    """
    child = dict(environment)
    for name, value in execution_environment(runtime, environment).items():
        if isinstance(value, UnsetType):
            child.pop(name, None)
        else:
            child[name] = value
    return child


def probe_host_generation(
    runtime: SnapshotRuntime,
    timeout_seconds: float | None = None,
) -> SetupEvidence:
    """Probe `runtime` against the host's own environment.

    This is the process-boundary composition point and the default
    `InstallHooks.probe`. Keeping the `os.environ` read here, rather than
    inside `probe_generation`, lets tests prove which environment a probe
    used without patching the process environment.
    """
    return probe_generation(runtime, timeout_seconds, environment=os.environ)
