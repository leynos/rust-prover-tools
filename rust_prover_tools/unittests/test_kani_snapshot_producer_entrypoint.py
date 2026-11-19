"""The producer entrypoint is the only way a workflow job acts.

Each subcommand reads the documents an earlier job wrote, checks that they
belong to the same run and attempt, and only then writes its own output. These
tests drive the real command line end to end: a manual resolution becomes a
plan, measured payloads become a manifest, and validation becomes a receipt.
Publication is exercised in `test_kani_snapshot_producer_publication`.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

import pytest

from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.producer.assemble import (
    assembly_input_to_json,
)
from rust_prover_tools.kani_snapshot.producer.entrypoint import build_parser, main
from rust_prover_tools.kani_snapshot.producer.lifecycle import parse_resolution
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.release_assets import PERFORMED_CHECKS
from rust_prover_tools.kani_snapshot.producer.resolution import (
    REQUIRED_UPSTREAM_CHECKS,
)
from rust_prover_tools.kani_snapshot.producer.validation_receipt import parse_receipt
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import pathlib

PINNED = pinned_recipe()
MANUAL_EVENT = "workflow_dispatch"


def _run_flags(
    *,
    run_id: int = 12345,
    run_attempt: int = 1,
    event: str = MANUAL_EVENT,
) -> list[str]:
    """Return the trusted run identity every subcommand requires."""
    return [
        "--run-id",
        str(run_id),
        "--run-attempt",
        str(run_attempt),
        "--event",
        event,
        "--run-url",
        f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    ]


def _checks() -> list[dict[str, object]]:
    """Return one passing record per required upstream check."""
    return [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "id": index + 1,
            "started_at": "2026-09-16T03:00:00Z",
        }
        for index, name in enumerate(REQUIRED_UPSTREAM_CHECKS)
    ]


def _write_checks(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write passing check evidence for the dispatched commit."""
    path = tmp_path / "checks.json"
    path.write_text(
        json.dumps({
            "commit": fixtures.UPSTREAM_COMMIT,
            "total_count": len(REQUIRED_UPSTREAM_CHECKS),
            "checks": _checks(),
        }),
        encoding="utf-8",
    )
    return path


def _resolve(tmp_path: pathlib.Path, *flags: str) -> int:
    """Resolve the pinned upstream commit by explicit manual input.

    The upstream tag is deliberately omitted: a dispatch names the commit it
    means to build, and the recipe owns which release that commit is.
    """
    return main([
        "resolve",
        "--route",
        "manual",
        "--commit",
        fixtures.UPSTREAM_COMMIT,
        "--checks",
        str(_write_checks(tmp_path)),
        "--output",
        str(tmp_path / "resolution.json"),
        *_run_flags(),
        *flags,
    ])


def _identity(tmp_path: pathlib.Path, *flags: str) -> int:
    """Adopt the dispatched publication identity."""
    return main([
        "identity",
        "--resolution",
        str(tmp_path / "resolution.json"),
        "--builder-commit",
        fixtures.BUILDER_COMMIT,
        "--publication-id",
        fixtures.TAG,
        "--output",
        str(tmp_path / "identity.json"),
        *_run_flags(),
        *flags,
    ])


def _build(tmp_path: pathlib.Path, *flags: str) -> int:
    """Derive a plan from the manual resolution and its identity."""
    return main([
        "build",
        "--resolution",
        str(tmp_path / "resolution.json"),
        "--identity",
        str(tmp_path / "identity.json"),
        "--builder-commit",
        fixtures.BUILDER_COMMIT,
        "--output",
        str(tmp_path / "plan.json"),
        *_run_flags(),
        *flags,
    ])


def _release(tmp_path: pathlib.Path) -> pathlib.Path:
    """Stage the payloads in the one directory the release will serve."""
    release = tmp_path / "release"
    fixtures.write_payloads(release)
    return release


def _assemble(tmp_path: pathlib.Path, *flags: str) -> int:
    """Assemble the pinned payloads into the release directory.

    The assembly input is the document a build job would hand over, written
    from the fixtures as the metadata job would write it.
    """
    inputs = tmp_path / "assembly-input.json"
    identity = fixtures.workflow(event=MANUAL_EVENT)
    inputs.write_bytes(
        assembly_input_to_json(fixtures.assembly_input(identity=identity))
    )
    release = tmp_path / "release"
    return main([
        "assemble",
        "--input",
        str(inputs),
        "--identity",
        str(tmp_path / "identity.json"),
        "--payloads",
        str(release),
        "--output",
        str(release),
        *_run_flags(),
        *flags,
    ])


def _attest(tmp_path: pathlib.Path) -> None:
    """Stand in for the attestation job's bundle about the assembled manifest."""
    release = tmp_path / "release"
    manifest = (release / "manifest.json").read_bytes()
    (release / "manifest.sigstore.jsonl").write_bytes(
        fixtures.attestation_bundle(hashlib.sha256(manifest).hexdigest())
    )


def _validate(tmp_path: pathlib.Path, *flags: str) -> int:
    """Write the validation receipt for the assembled manifest."""
    return main([
        "validate",
        "--manifest",
        str(tmp_path / "release" / "manifest.json"),
        "--payloads",
        str(tmp_path / "release"),
        "--output",
        str(tmp_path / "receipt.json"),
        *_run_flags(),
        *flags,
    ])


def _prepare(tmp_path: pathlib.Path) -> None:
    """Run resolve, identity, build, assemble, attest and validate."""
    _release(tmp_path)
    assert _resolve(tmp_path) == 0, "resolve must succeed before identity"
    assert _identity(tmp_path) == 0, "identity must succeed before build"
    assert _build(tmp_path) == 0, "build must succeed before assembly"
    assert _assemble(tmp_path) == 0, "assemble must succeed after build"
    _attest(tmp_path)
    assert _validate(tmp_path) == 0, "validate must succeed for the assembly"


def test_validate_records_only_the_checks_it_performed(
    tmp_path: pathlib.Path,
) -> None:
    """The receipt names the code's checks, not a caller-supplied label list."""
    _prepare(tmp_path)

    receipt = parse_receipt((tmp_path / "receipt.json").read_bytes())

    assert receipt.checks == PERFORMED_CHECKS, (
        "the receipt must record only the checks validation performed"
    )


def test_validate_no_longer_accepts_a_checks_override(
    tmp_path: pathlib.Path,
) -> None:
    """A label list on the command line could claim checks never performed."""
    with pytest.raises(SystemExit):
        main([
            "validate",
            "--manifest",
            str(tmp_path / "release" / "manifest.json"),
            "--payloads",
            str(tmp_path / "release"),
            "--checks",
            "manifest-digest",
            "--output",
            str(tmp_path / "receipt.json"),
            *_run_flags(),
        ])


def test_a_scheduled_resolution_selects_the_newest_qualified_release(
    tmp_path: pathlib.Path,
) -> None:
    """The scheduled route consumes the collector's evidence, not an input."""
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps([
            {
                "release": {
                    "tag_name": PINNED.upstream.tag,
                    "published_at": "2026-09-16T03:43:14Z",
                    "draft": False,
                    "prerelease": False,
                },
                "tag_ref": {
                    "ref": f"refs/tags/{PINNED.upstream.tag}",
                    "object": {"type": "commit", "sha": fixtures.UPSTREAM_COMMIT},
                },
                "total_count": len(REQUIRED_UPSTREAM_CHECKS),
                "checks": _checks(),
            },
        ]),
        encoding="utf-8",
    )
    output = tmp_path / "resolution.json"
    code = main([
        "resolve",
        "--route",
        "scheduled",
        "--candidates",
        str(candidates),
        "--output",
        str(output),
        *_run_flags(event="schedule"),
    ])
    assert code == 0, "scheduled resolve must accept qualified evidence"
    resolution = parse_resolution(output.read_bytes())
    assert resolution.route.value == "scheduled", "route must be scheduled"
    assert resolution.upstream_commit == fixtures.UPSTREAM_COMMIT, (
        "resolution must select the qualified commit"
    )
    assert resolution.upstream_tag == PINNED.upstream.tag, (
        "resolution must adopt the pinned tag"
    )


def test_a_manual_resolution_requires_a_commit(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dispatch that names no commit has nothing to build."""
    code = main([
        "resolve",
        "--route",
        "manual",
        "--output",
        str(tmp_path / "resolution.json"),
        *_run_flags(),
    ])
    assert code == 1, "manual resolve must fail without a commit"
    assert "--commit" in capsys.readouterr().err, (
        "the failure must name the missing --commit"
    )


def test_a_manual_resolution_adopts_the_pinned_upstream_tag(
    tmp_path: pathlib.Path,
) -> None:
    """The recipe owns the release a commit belongs to, not the dispatch.

    A dispatch input can only name the commit; accepting a tag from it would
    let a caller pair one release's tag with another release's source.
    """
    assert _resolve(tmp_path) == 0, "resolve must succeed before reading output"

    resolution = parse_resolution((tmp_path / "resolution.json").read_bytes())
    assert resolution.upstream_commit == fixtures.UPSTREAM_COMMIT, (
        "resolution must record the dispatched commit"
    )
    assert resolution.upstream_tag == PINNED.upstream.tag, (
        "resolution must adopt the recipe's tag"
    )


def test_a_scheduled_resolution_requires_candidates(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without collected evidence the scheduled route cannot choose."""
    code = main([
        "resolve",
        "--route",
        "scheduled",
        "--output",
        str(tmp_path / "resolution.json"),
        *_run_flags(event="schedule"),
    ])
    assert code == 1, "scheduled resolve must fail without candidates"
    assert "--candidates" in capsys.readouterr().err, (
        "the failure must name the missing --candidates"
    )


def test_build_refuses_a_resolution_from_another_run(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale resolution must not authorise this run's build."""
    assert _resolve(tmp_path) == 0, "resolve must succeed before identity"
    assert _identity(tmp_path) == 0, "identity must succeed before build"
    code = _build(tmp_path, *_run_flags(run_id=999))
    assert code == 1, "build must refuse a resolution from another run"
    assert "another workflow run" in capsys.readouterr().err, (
        "the refusal must name the run mismatch"
    )


def test_validate_refuses_a_manifest_from_another_attempt(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rerun revalidates rather than inheriting the previous verdict."""
    _prepare(tmp_path)
    code = _validate(tmp_path, *_run_flags(run_attempt=2))
    assert code == 1, "validate must refuse a manifest from another attempt"
    assert "another workflow run" in capsys.readouterr().err, (
        "the refusal must name the attempt mismatch"
    )


def test_validate_refuses_a_tampered_payload(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validation hashes the files it is given, not the manifest's claims."""
    _prepare(tmp_path)
    (tmp_path / "release" / fixtures.names()["backend"]).write_bytes(b"tampered")
    code = _validate(tmp_path)
    assert code == 1, "validate must refuse a tampered payload"
    assert "does not match the manifest" in capsys.readouterr().err, (
        "the refusal must name the digest mismatch"
    )


def test_the_parser_requires_a_subcommand() -> None:
    """Running the module with no operation is a usage error, not a no-op."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
