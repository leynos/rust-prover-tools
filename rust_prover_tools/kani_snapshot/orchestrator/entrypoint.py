"""Developer entrypoint for the guarded producer workflow.

Run `python -m rust_prover_tools.kani_snapshot.orchestrator.entrypoint --help` for
the argument list of each command. `resolve` selects an upstream commit,
`identity` records the immutable tag, `build` derives the pinned plan,
`assemble` hashes the payloads into a manifest, `validate` writes same-run
evidence, and `publish` drives the immutable release.

These are internal automation interfaces, not consumer APIs, so they favour
failing closed over convenience. The operations themselves live in
`operations`, which keeps this module a thin dispatch table.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release.api import GhReleaseApi
from rust_prover_tools.kani_snapshot.orchestrator.operations import (
    TRUSTED_EVENTS,
    assemble,
    build,
    identity,
    publish,
    resolve,
    validate,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import ResolutionRoute

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("main",)


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the trusted run identity every operation requires."""
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--event", choices=TRUSTED_EVENTS, required=True)
    parser.add_argument("--run-url", default=None)


def _publish_with_github(namespace: argparse.Namespace) -> int:
    """Publish through the real GitHub adapter.

    This is the composition root. Every other operation composes its effect
    from documents and local files; publication is the one that needs a live
    release API, so the adapter is chosen here and nowhere else.
    """
    return publish(namespace, GhReleaseApi())


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for every producer operation."""
    parser = argparse.ArgumentParser(prog="kani-snapshot-producer")
    commands = parser.add_subparsers(dest="operation", required=True)

    resolve_parser = commands.add_parser("resolve", help="select an upstream commit")
    _add_workflow_arguments(resolve_parser)
    resolve_parser.add_argument(
        "--route", choices=[route.value for route in ResolutionRoute], required=True
    )
    resolve_parser.add_argument("--commit", default=None)
    resolve_parser.add_argument(
        "--upstream-tag",
        default=None,
        help="upstream release tag; defaults to the pinned recipe's tag",
    )
    resolve_parser.add_argument("--candidates", default=None)
    resolve_parser.add_argument(
        "--checks",
        default=None,
        help="observed check evidence for a manual dispatch's commit",
    )
    resolve_parser.add_argument("--output", required=True)
    resolve_parser.set_defaults(handler=resolve)

    identity_parser = commands.add_parser(
        "identity", help="allocate or adopt the immutable publication identity"
    )
    _add_workflow_arguments(identity_parser)
    identity_parser.add_argument("--resolution", required=True)
    identity_parser.add_argument("--builder-commit", required=True)
    identity_parser.add_argument("--month", default=None)
    identity_parser.add_argument("--releases", default=None)
    identity_parser.add_argument("--publication-id", default=None)
    identity_parser.add_argument("--output", required=True)
    identity_parser.set_defaults(handler=identity)

    plan_parser = commands.add_parser("build", help="derive the pinned build plan")
    _add_workflow_arguments(plan_parser)
    plan_parser.add_argument("--resolution", required=True)
    plan_parser.add_argument("--identity", required=True)
    plan_parser.add_argument("--builder-commit", required=True)
    plan_parser.add_argument("--output", required=True)
    plan_parser.set_defaults(handler=build)

    assembly_parser = commands.add_parser(
        "assemble", help="hash payloads into a manifest"
    )
    _add_workflow_arguments(assembly_parser)
    assembly_parser.add_argument("--input", required=True)
    assembly_parser.add_argument(
        "--identity",
        required=True,
        help="identity the resolve job allocated for this run",
    )
    assembly_parser.add_argument("--payloads", required=True)
    assembly_parser.add_argument("--output", required=True)
    assembly_parser.set_defaults(handler=assemble)

    validate_parser = commands.add_parser(
        "validate", help="write same-run validation evidence"
    )
    _add_workflow_arguments(validate_parser)
    validate_parser.add_argument("--manifest", required=True)
    validate_parser.add_argument("--payloads", required=True)
    validate_parser.add_argument("--output", required=True)
    validate_parser.set_defaults(handler=validate)

    publish_parser = commands.add_parser(
        "publish", help="publish the immutable release"
    )
    _add_workflow_arguments(publish_parser)
    publish_parser.add_argument("--manifest", required=True)
    publish_parser.add_argument("--receipt", required=True)
    publish_parser.add_argument("--payloads", required=True)
    publish_parser.set_defaults(handler=_publish_with_github)
    return parser


#: What writing to a standard error that has gone away can raise. Named
#: because the pylint gate parses this module with Python 3.12, where a bare
#: `except OSError, ValueError:` is a syntax error rather than a tuple.
_STREAM_GONE = (OSError, ValueError)


class _StderrEvents(logging.Handler):
    """The entrypoint's own event handler, distinguishable from a host's.

    A named type rather than a marker attribute: `_configure_logging` can then
    tell whether the handler is already installed without inspecting private
    attributes it does not own.

    The stream is resolved at emit time rather than held from construction.
    A process that redirects standard error after importing this module — a
    test runner capturing output, for one — would otherwise leave the handler
    writing to a stream nobody reads.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record to the current standard error.

        A stream that has gone away is dropped rather than reported.
        `handleError` writes its diagnostic to the same stream, and Python
        suppresses only `OSError` from that write, so a closed text stream's
        `ValueError` would escape and abort the operation the record was
        describing.
        """
        # pragma: no cover - the suppressed branch needs a closed stream
        with contextlib.suppress(*_STREAM_GONE):
            sys.stderr.write(f"{self.format(record)}\n")


class _EventFormatter(logging.Formatter):
    """Render an event's bounded fields beside its name.

    The record's message is the event name; its fields live in `event_data`,
    and a run log that showed only the name would still hide the step, the
    action and the outcome a maintainer needs. Fields are appended in the
    order they were recorded, as ``key=value`` pairs.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the event name followed by its recorded fields."""
        rendered = super().format(record)
        event_data = getattr(record, "event_data", None)
        if not isinstance(event_data, dict):
            return rendered
        fields = " ".join(
            f"{key}={value}" for key, value in event_data.items() if key != "event"
        )
        return f"{rendered} {fields}".rstrip()


def _configure_logging() -> None:
    """Send the library's structured events to standard error.

    The library installs no handlers, so a hosted job would otherwise report
    only the final error: a transient retry, a state transition or the ending
    of a publication would never reach the run log. The handler belongs to this
    CLI entrypoint rather than to a library import, so embedding the package
    does not silently reconfigure the host application's logging.

    The level is DEBUG because `log_event` emits at debug level. Attaching the
    handler is idempotent: `main` runs once per subcommand process, but a test
    or an embedding caller may invoke it more than once. The level is set on
    every call rather than only when the handler is installed, because a host
    that raised it between calls would otherwise silence every event.
    """
    logger = logging.getLogger("rust_prover_tools")
    logger.setLevel(logging.DEBUG)
    if any(isinstance(handler, _StderrEvents) for handler in logger.handlers):
        return
    handler = _StderrEvents()
    handler.setFormatter(_EventFormatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run one producer operation and report a user-facing failure."""
    _configure_logging()
    namespace = build_parser().parse_args(argv)
    try:
        return typ.cast("cabc.Callable[[argparse.Namespace], int]", namespace.handler)(
            namespace
        )
    except ProverToolError as exc:
        print(f"kani-snapshot-producer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
