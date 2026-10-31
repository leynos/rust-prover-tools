"""Platform checks for prebuilt prover binaries."""

from __future__ import annotations

import platform

from rust_prover_tools.errors import ProverToolError

__all__ = ("require_linux_x86_64",)

SUPPORTED_MACHINES: frozenset[str] = frozenset({"x86_64", "amd64"})


def require_linux_x86_64(
    tool_name: str,
    *,
    fallback_hint: str | None = None,
) -> None:
    """Reject hosts that cannot run the pinned Linux x86_64 binaries.

    Parameters
    ----------
    tool_name : str
        Tool name used in the error message.
    fallback_hint : str | None
        Optional sentence appended to the error, such as an instruction to
        compile from source.

    Returns
    -------
    None
        Returns when the host is Linux x86_64.

    Raises
    ------
    ProverToolError
        Raised when the host is not Linux x86_64.
    """
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in SUPPORTED_MACHINES:
        return
    msg = (
        f"prebuilt {tool_name} binaries are only available for Linux x86_64; "
        f"this host is {system} {machine}."
    )
    if fallback_hint:
        msg = f"{msg} {fallback_hint}"
    raise ProverToolError(msg)
