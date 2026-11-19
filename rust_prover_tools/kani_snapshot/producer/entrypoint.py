"""Developer entrypoint for the guarded producer workflow.

Run `python -m rust_prover_tools.kani_snapshot.producer.entrypoint --help` for
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
import sys
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.lifecycle import ResolutionRoute
from rust_prover_tools.kani_snapshot.producer.operations import (
    TRUSTED_EVENTS,
    assemble,
    build,
    identity,
    publish,
    resolve,
    validate,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("main",)


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the trusted run identity every operation requires."""
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--event", choices=TRUSTED_EVENTS, required=True)
    parser.add_argument("--run-url", default=None)


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
    validate_parser.add_argument(
        "--checks", default="manifest-digest,payload-digest,checksums,attestation"
    )
    validate_parser.add_argument("--output", required=True)
    validate_parser.set_defaults(handler=validate)

    publish_parser = commands.add_parser(
        "publish", help="publish the immutable release"
    )
    _add_workflow_arguments(publish_parser)
    publish_parser.add_argument("--manifest", required=True)
    publish_parser.add_argument("--receipt", required=True)
    publish_parser.add_argument("--payloads", required=True)
    publish_parser.set_defaults(handler=publish)
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run one producer operation and report a user-facing failure."""
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
