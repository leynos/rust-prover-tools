"""Open and validate the CLI's evidence destinations.

The option models in ``kani_harness_cli_options`` describe what the user
asked for; this adapter is where those values meet the filesystem. It owns the
alias checks, the standard-output comparison, and the handles a run appends to,
so the models stay free of filesystem access. Opening is the library's
`prepare_and_open_diagnostics`, which names its own write and repairs a partial
trailing JSONL record through `prepare_jsonl_destination` before opening the
sink. `open_destinations` and the sink constructors never write.
"""

from __future__ import annotations

import os
import sys
import typing as typ

from rust_prover_tools.errors import StdoutIdentityError
from rust_prover_tools.supervision import (
    DiagnosticSinks,
    SupervisionConfig,
    require_distinct_destinations,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

__all__ = ("require_stdout_free", "supervision_policy")


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
    threshold = policy.watchdog_mib * 1024 * 1024 if policy.watchdog else None
    return SupervisionConfig(
        timeout_seconds=policy.timeout_seconds,
        sample_interval_seconds=policy.sample_interval_seconds,
        heartbeat_interval_seconds=policy.heartbeat_seconds,
        watchdog_threshold_bytes=threshold,
        max_log_bytes=policy.max_log_mib * 1024 * 1024,
        sinks=DiagnosticSinks(
            human_path=destinations.human,
            jsonl_path=destinations.jsonl,
            report_path=destinations.report,
        ),
    )


#: `fileno` failures that mean the stream has no descriptor at all. Named
#: apart from the `try` so the formatter cannot rewrite it to PEP 758 syntax,
#: which the pinned pylint interpreter cannot parse.
_NO_STDOUT_IDENTITY: typ.Final[tuple[type[BaseException], ...]] = (
    AttributeError,
    ValueError,
)


def require_stdout_free(path: Path | None, option: str) -> None:
    """Refuse a destination that is also the standard-output file.

    An in-memory or captured standard output has no descriptor, so it has no
    file identity and cannot alias a path destination; that case is a
    documented no-op rather than an error, because a test harness replaces
    standard output with exactly such an object. A real descriptor that cannot
    be inspected is different: the check then cannot be made, and the
    destination is refused instead of allowed.

    Parameters
    ----------
    path : Path | None
        Destination to compare, or `None` when the role is unset.
    option : str
        Option name, used in the refusal message.

    Raises
    ------
    ValueError
        The destination resolves to the same file as standard output.
    StdoutIdentityError
        Standard output or an existing destination cannot be inspected.
    """
    if path is None:
        return
    try:
        fileno = sys.stdout.fileno()
    except _NO_STDOUT_IDENTITY:
        # A capture object or an in-memory stream: no descriptor, no identity,
        # and therefore nothing a path could alias.
        return
    try:
        stream = os.fstat(fileno)
    except OSError as error:
        msg = f"standard output cannot be inspected: {error}"
        raise StdoutIdentityError(msg) from error
    try:
        target = path.stat()
    except FileNotFoundError:
        return
    except OSError as error:
        msg = f"the {option} destination cannot be inspected: {error}"
        raise StdoutIdentityError(msg) from error
    # The device-and-inode comparison covers a pipe as well as a regular
    # file: `--jsonl /dev/stdout` on a pipeline aliases the very stream the
    # human log and final table are written to.
    if (stream.st_dev, stream.st_ino) == (target.st_dev, target.st_ino):
        msg = f"standard output already names the {option} destination: {path}"
        raise ValueError(msg)
