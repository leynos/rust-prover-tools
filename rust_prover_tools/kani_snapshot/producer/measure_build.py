"""Measure the build job's toolchain, backend and cache evidence.

The build job is the only place these identities can be observed, and it is
also the least trusted job: it compiles upstream source. So the measurement is
kept here, in tested code, and it records only things it reads from bytes on
disk or from a program it ran. Nothing is inferred from a version string.

The module deliberately does not decide *which* libraries or lockfiles matter.
The job names them; this module hashes exactly those paths, records them
relative to the sysroot where that is meaningful, and refuses to write an
evidence document the manifest parser would later reject.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import (
    BackendIdentity,
    CacheObservation,
    SolverRecord,
    SubmoduleRecord,
)
from rust_prover_tools.kani_snapshot.producer.build_evidence import (
    EVIDENCE_SCHEMA_VERSION,
    BuildEvidence,
    evidence_to_json,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import write_document
from rust_prover_tools.kani_snapshot.toolchain_model import (
    FileRecord,
    ToolchainIdentity,
)
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_non_negative_int,
    require_number,
    require_sha256,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("main", "measure")

#: The components file rustup installs for each toolchain.
_COMPONENTS: typ.Final[str] = "lib/rustlib/components"


def _run(argv: cabc.Sequence[str], what: str) -> str:
    """Run one measurement command and return its stripped standard output."""
    result = run_command(CommandSpec(tuple(argv)))
    if result.exit_code != 0:
        msg = f"failed to read {what}: {result.stderr.strip()}"
        raise ProverToolError(msg)
    return result.stdout.strip()


def _require_file(path: Path, what: str) -> Path:
    """Require a regular file before anything is measured from it."""
    if not path.is_file():
        msg = f"{what} '{path}' is not a regular file"
        raise ProverToolError(msg)
    return path


def _library_records(
    sysroot: Path, libraries: cabc.Sequence[str]
) -> tuple[FileRecord, ...]:
    """Hash the recorded toolchain libraries, relative to the sysroot."""
    if not libraries:
        msg = "build measurement requires at least one --library"
        raise ProverToolError(msg)
    records: list[FileRecord] = []
    for relative in libraries:
        path = _require_file(sysroot / relative, f"toolchain library {relative!r}")
        records.append(FileRecord(path=relative, sha256=sha256_file(path)))
    return tuple(records)


def _lock_digests(paths: cabc.Sequence[Path]) -> tuple[tuple[str, str], ...]:
    """Hash each recorded lockfile, keyed by its path as given."""
    return tuple(
        (str(path), sha256_file(_require_file(path, f"lockfile '{path}'")))
        for path in paths
    )


def _solver_records(values: cabc.Sequence[str]) -> tuple[SolverRecord, ...]:
    """Parse `name=version=sha256` solver measurements."""
    records: list[SolverRecord] = []
    for value in values:
        name, separator, rest = value.partition("=")
        version, second, digest = rest.partition("=")
        if not separator or not second:
            msg = f"solver measurement {value!r} must be name=version=sha256"
            raise ProverToolError(msg)
        records.append(
            SolverRecord(
                name=require_truthy_text(name, "solver name"),
                version=require_truthy_text(version, "solver version"),
                sha256=require_sha256(digest, "solver sha256"),
            )
        )
    return tuple(sorted(records, key=lambda record: record.name))


def _submodule_records(values: cabc.Sequence[str]) -> tuple[SubmoduleRecord, ...]:
    """Parse `path=commit` submodule measurements read from the checkout."""
    if not values:
        msg = "build measurement requires at least one --submodule"
        raise ProverToolError(msg)
    records: list[SubmoduleRecord] = []
    for value in values:
        path, _, commit = value.rpartition("=")
        if not path or not commit:
            msg = f"submodule measurement {value!r} must be path=commit"
            raise ProverToolError(msg)
        records.append(
            SubmoduleRecord(
                path=require_truthy_text(path, "submodule path"),
                commit=require_full_sha(commit, "submodule commit"),
            )
        )
    return tuple(sorted(records, key=lambda record: record.path))


def measure(namespace: argparse.Namespace) -> BuildEvidence:
    """Measure one build job's evidence from the paths it was given.

    Parameters
    ----------
    namespace : argparse.Namespace
        Parsed command line naming the sysroot, compilers, lockfiles and
        resources.

    Returns
    -------
    BuildEvidence
        Measurements the manifest parser accepts.

    Raises
    ------
    ProverToolError
        Raised when a named file is missing, a command fails, or a recorded
        identity is malformed.
    """
    sysroot = pathlib.Path(namespace.sysroot)
    rustc = _require_file(sysroot / "bin" / "rustc", "rustc")
    rustc_vv = _run([str(rustc), "-vV"], "rustc -vV")
    kani_compiler_path = _require_file(
        pathlib.Path(namespace.kani_compiler), "kani-compiler"
    )
    kani_compiler = _run(
        [str(kani_compiler_path), "--version"], "kani-compiler --version"
    )
    toolchain = ToolchainIdentity(
        identifier=sysroot.name,
        rustc_vv=rustc_vv,
        kani_compiler=kani_compiler,
        channel_metadata_sha256=sha256_file(
            _require_file(pathlib.Path(namespace.channel_manifest), "channel metadata")
        ),
        components=(),
        rustc_sha256=sha256_file(rustc),
        rustup_components_sha256=sha256_file(
            _require_file(sysroot / _COMPONENTS, "rustup components list")
        ),
        libraries=_library_records(sysroot, namespace.library),
    )
    backend = BackendIdentity(
        version=require_truthy_text(namespace.backend_version, "backend version"),
        revision=require_full_sha(namespace.upstream_commit, "backend revision"),
        solvers=_solver_records(namespace.solver),
    )
    return BuildEvidence(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        toolchain=toolchain,
        backend=backend,
        launcher_package_version=require_truthy_text(
            namespace.launcher_package_version, "launcher package version"
        ),
        lock_digests=_lock_digests([pathlib.Path(path) for path in namespace.lock]),
        submodules=_submodule_records(namespace.submodule),
        cache=CacheObservation(
            outcome=require_truthy_text(namespace.cache_outcome, "cache outcome"),
            key=namespace.cache_key,
        ),
        wall_seconds=require_number(namespace.wall_seconds, "wall_seconds"),
        disk_high_water_bytes=require_non_negative_int(
            namespace.disk_high_water_bytes, "disk_high_water_bytes"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the build-measurement command line parser."""
    parser = argparse.ArgumentParser(
        prog="kani-snapshot-measure-build",
        description="Measure the evidence the assembly input needs.",
    )
    parser.add_argument("--sysroot", required=True)
    parser.add_argument("--channel-manifest", required=True)
    parser.add_argument("--kani-compiler", required=True)
    parser.add_argument("--backend-version", required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--launcher-package-version", required=True)
    parser.add_argument("--library", action="append", default=[])
    parser.add_argument("--lock", action="append", default=[])
    parser.add_argument(
        "--submodule",
        action="append",
        default=[],
        help="recursive submodule revision observed in the checkout, path=commit",
    )
    parser.add_argument("--solver", action="append", default=[])
    parser.add_argument("--cache-outcome", required=True)
    parser.add_argument("--cache-key", default=None)
    parser.add_argument("--wall-seconds", required=True, type=float)
    parser.add_argument("--disk-high-water-bytes", required=True, type=int)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write one build job's evidence document."""
    namespace = build_parser().parse_args(argv)
    try:
        write_document(
            pathlib.Path(namespace.output), evidence_to_json(measure(namespace))
        )
    except (ProverToolError, OSError) as exc:
        print(f"kani-snapshot-measure-build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the module entry
    raise SystemExit(main())
