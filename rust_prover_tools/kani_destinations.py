"""Open and validate the CLI's evidence destinations.

The option models in ``kani_harness_cli_options`` describe what the user
asked for; this adapter is where those values meet the filesystem. It owns the
alias checks, the standard-output comparison, and the handles a run appends to,
so the models stay free of filesystem access.
"""

from __future__ import annotations

import os
import sys
import typing as typ

from rust_prover_tools.kani_diagnostics import open_destinations
from rust_prover_tools.supervision import (
    Diagnostics,
    DiagnosticSinks,
    SupervisionConfig,
    require_distinct_destinations,
)

if typ.TYPE_CHECKING:
    import contextlib
    from pathlib import Path

    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

__all__ = (
    "open_run_diagnostics",
    "require_stdout_free",
    "supervision_policy",
)


def supervision_policy(
    policy: PolicyFlags,
    destinations: DestinationFlags,
) -> SupervisionConfig:
    """Translate the CLI flags into the validated policy.

    Parameters
    ----------
    policy : PolicyFlags
        Per-harness budget, sampling, heartbeat, and watchdog settings.
    destinations : DestinationFlags
        Evidence and report destinations.

    Returns
    -------
    SupervisionConfig
        The validated policy.

    Raises
    ------
    ValueError
        A timing is unusable or two destinations name one file.
    """
    require_distinct_destinations(
        human_path=destinations.human,
        jsonl_path=destinations.jsonl,
        report_path=destinations.report,
    )
    require_stdout_free(destinations.jsonl, "--jsonl")
    require_stdout_free(destinations.report, "--report")
    threshold = policy.watchdog_mib * 1024 * 1024 if policy.watchdog else None
    return SupervisionConfig(
        timeout_seconds=policy.timeout_seconds,
        sample_interval_seconds=policy.sample_interval_seconds,
        heartbeat_interval_seconds=policy.heartbeat_seconds,
        watchdog_threshold_bytes=threshold,
        max_log_bytes=policy.max_log_mib * 1024 * 1024,
        sinks=DiagnosticSinks(report_path=destinations.report),
    )


def open_run_diagnostics(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
    destinations: DestinationFlags,
) -> Diagnostics:
    """Open the live diagnostic destinations for one run.

    Parameters
    ----------
    stack : contextlib.ExitStack
        Stack that owns every handle opened here.
    config : SupervisionConfig
        Validated policy supplying the line bound.
    destinations : DestinationFlags
        Human and JSONL destinations, or `None` for standard output and off.

    Returns
    -------
    Diagnostics
        The sinks for this run.
    """
    return open_destinations(
        stack,
        DiagnosticSinks(
            human_path=destinations.human,
            jsonl_path=destinations.jsonl,
        ),
        human_stream=sys.stdout,
        max_line_bytes=config.max_line_bytes,
    )


def require_stdout_free(path: Path | None, option: str) -> None:
    """Refuse a destination that is also the standard-output file."""
    if path is None:
        return
    try:
        stream = os.fstat(sys.stdout.fileno())
        target = path.stat()
    except (AttributeError, OSError, ValueError):  # fmt: skip
        return
    # The device-and-inode comparison covers a pipe as well as a regular
    # file: `--jsonl /dev/stdout` on a pipeline aliases the very stream the
    # human log and final table are written to.
    if (stream.st_dev, stream.st_ino) == (target.st_dev, target.st_ino):
        msg = f"standard output already names the {option} destination: {path}"
        raise ValueError(msg)
