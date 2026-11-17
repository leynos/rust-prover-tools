"""The producer entrypoint is the only way a workflow job acts.

Each subcommand reads the documents an earlier job wrote, checks that they
belong to the same run and attempt, and only then writes its own output. These
tests drive the real command line end to end: a manual resolution becomes a
plan, measured payloads become a manifest, validation becomes a receipt, and
publication is refused before any release call when the evidence does not bind.
The final test runs the whole publication through a modelled `gh`.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import typing as typ

import pytest

from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.models import REPOSITORY, SUPPORTED_TARGET
from rust_prover_tools.kani_snapshot.producer.assemble import (
    assembly_input_to_json,
)
from rust_prover_tools.kani_snapshot.producer.entrypoint import build_parser, main
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    parse_build_plan,
    parse_resolution,
)
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    parse_publication_identity,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.producer.release_assets import EVIDENCE_NAMES
from rust_prover_tools.kani_snapshot.producer.resolution import (
    REQUIRED_UPSTREAM_CHECKS,
)
from rust_prover_tools.kani_snapshot.producer.validation_receipt import (
    parse_receipt,
    verify_receipt,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)

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


def _resolve(tmp_path: pathlib.Path, *flags: str) -> int:
    """Resolve the pinned upstream commit by explicit manual input."""
    return main([
        "resolve",
        "--route",
        "manual",
        "--commit",
        fixtures.UPSTREAM_COMMIT,
        "--upstream-tag",
        PINNED.upstream.tag,
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


def _write_assembly_input(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write the metadata document a build job would hand to assembly."""
    path = tmp_path / "assembly-input.json"
    identity = fixtures.workflow(event=MANUAL_EVENT)
    path.write_bytes(assembly_input_to_json(fixtures.assembly_input(identity=identity)))
    return path


def _release(tmp_path: pathlib.Path) -> pathlib.Path:
    """Stage the payloads in the one directory the release will serve."""
    release = tmp_path / "release"
    fixtures.write_payloads(release)
    return release


def _assemble(tmp_path: pathlib.Path, *flags: str) -> int:
    """Assemble the pinned payloads into the release directory."""
    release = tmp_path / "release"
    return main([
        "assemble",
        "--input",
        str(_write_assembly_input(tmp_path)),
        "--payloads",
        str(release),
        "--output",
        str(release),
        *_run_flags(),
        *flags,
    ])


def _attest(tmp_path: pathlib.Path) -> None:
    """Stand in for the attestation job's bundle."""
    (tmp_path / "release" / "manifest.sigstore.jsonl").write_bytes(
        b'{"mediaType": "application/vnd.dev.sigstore.bundle+json"}\n'
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
    assert _resolve(tmp_path) == 0
    assert _identity(tmp_path) == 0
    assert _build(tmp_path) == 0
    assert _assemble(tmp_path) == 0
    _attest(tmp_path)
    assert _validate(tmp_path) == 0


def test_the_entrypoint_runs_the_whole_producer_sequence(
    tmp_path: pathlib.Path,
) -> None:
    """Each job's output parses and binds to the run that produced it."""
    _prepare(tmp_path)
    resolution = parse_resolution((tmp_path / "resolution.json").read_bytes())
    identity = parse_publication_identity((tmp_path / "identity.json").read_bytes())
    plan = parse_build_plan((tmp_path / "plan.json").read_bytes())
    parsed = parse_manifest((tmp_path / "release" / "manifest.json").read_bytes())
    receipt = parse_receipt((tmp_path / "receipt.json").read_bytes())
    assert resolution.route.value == "manual"
    assert identity.tag == fixtures.TAG
    assert identity.upstream_commit == resolution.upstream_commit
    assert plan.upstream_commit == resolution.upstream_commit
    assert plan.tag == fixtures.TAG
    assert parsed.manifest.tag == plan.tag
    assert parsed.manifest.builder_commit == fixtures.BUILDER_COMMIT
    assert parsed.manifest.workflow == fixtures.workflow(event=MANUAL_EVENT)
    assert parsed.manifest.target == SUPPORTED_TARGET
    verify_receipt(receipt, parsed)


def test_the_entrypoint_writes_checksums_beside_the_manifest(
    tmp_path: pathlib.Path,
) -> None:
    """`SHA256SUMS` covers the payloads and the manifest bytes it names."""
    _prepare(tmp_path)
    manifest = (tmp_path / "release" / "manifest.json").read_bytes()
    rows = {
        name: digest
        for digest, name in (
            line.split("  ", 1)
            for line in (tmp_path / "release" / "SHA256SUMS")
            .read_text(encoding="ascii")
            .splitlines()
        )
    }
    assert rows["manifest.json"] == hashlib.sha256(manifest).hexdigest()
    for name in fixtures.names().values():
        assert name in rows


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
                "checks": [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                        "id": index + 1,
                        "started_at": "2026-09-16T03:00:00Z",
                    }
                    for index, name in enumerate(REQUIRED_UPSTREAM_CHECKS)
                ],
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
    assert code == 0
    resolution = parse_resolution(output.read_bytes())
    assert resolution.route.value == "scheduled"
    assert resolution.upstream_commit == fixtures.UPSTREAM_COMMIT
    assert resolution.upstream_tag == PINNED.upstream.tag


def test_a_manual_resolution_requires_a_commit_and_tag(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Half a manual identity is not a resolution."""
    code = main([
        "resolve",
        "--route",
        "manual",
        "--output",
        str(tmp_path / "resolution.json"),
        *_run_flags(),
    ])
    assert code == 1
    assert "--commit and --upstream-tag" in capsys.readouterr().err


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
    assert code == 1
    assert "--candidates" in capsys.readouterr().err


def test_build_refuses_a_resolution_from_another_run(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale resolution must not authorise this run's build."""
    assert _resolve(tmp_path) == 0
    assert _identity(tmp_path) == 0
    code = _build(tmp_path, *_run_flags(run_id=999))
    assert code == 1
    assert "another workflow run" in capsys.readouterr().err


def test_validate_refuses_a_manifest_from_another_attempt(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rerun revalidates rather than inheriting the previous verdict."""
    _release(tmp_path)
    assert _resolve(tmp_path) == 0
    assert _identity(tmp_path) == 0
    assert _build(tmp_path) == 0
    assert _assemble(tmp_path) == 0
    _attest(tmp_path)
    code = _validate(tmp_path, *_run_flags(run_attempt=2))
    assert code == 1
    assert "another workflow run" in capsys.readouterr().err


def test_validate_refuses_a_tampered_payload(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validation hashes the files it is given, not the manifest's claims."""
    _release(tmp_path)
    assert _resolve(tmp_path) == 0
    assert _identity(tmp_path) == 0
    assert _build(tmp_path) == 0
    assert _assemble(tmp_path) == 0
    _attest(tmp_path)
    (tmp_path / "release" / fixtures.names()["backend"]).write_bytes(b"tampered")
    code = _validate(tmp_path)
    assert code == 1
    assert "does not match the manifest" in capsys.readouterr().err


def test_publish_refuses_a_receipt_from_another_run_before_any_call(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cross-run receipt is refused before the release API is touched."""
    _prepare(tmp_path)
    calls: list[list[str]] = []

    def record(invocation: Invocation) -> tuple[str, str, int]:
        """Record any accidental release call and answer nothing useful."""
        calls.append(invocation.args)
        return ("", "", 0)

    cmd_mox.stub("gh").runs(record)
    code = main([
        "publish",
        "--manifest",
        str(tmp_path / "release" / "manifest.json"),
        "--receipt",
        str(tmp_path / "receipt.json"),
        "--payloads",
        str(tmp_path / "release"),
        *_run_flags(run_id=999),
    ])
    assert code == 1
    assert "another workflow run or attempt" in capsys.readouterr().err
    assert not calls


class FakeGitHub:
    """A minimal release server that `gh` is stubbed to answer with.

    It stores uploaded bytes and serves them again on download, so the digest
    that releases publication is computed from what the release would really
    serve. Every other call shape raises, which catches an adapter drift.
    """

    def __init__(self) -> None:
        """Create an absent release."""
        self.exists = False
        self.is_draft = False
        self.assets: dict[str, bytes] = {}
        self.target = ""
        self.published = False

    def document(self) -> str:
        """Return the release as `gh api --jq` would print it."""
        return json.dumps({
            "id": 4242,
            "tag_name": fixtures.TAG,
            "draft": self.is_draft,
            "target_commitish": self.target,
            "assets": [
                {
                    "name": name,
                    "size": len(data),
                    "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
                }
                for name, data in sorted(self.assets.items())
            ],
        })

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh` invocation as the release API would."""
        args = invocation.args
        if args[0] == "api":
            return self._api(args)
        if args[0] == "release":
            return self._release(args)
        msg = f"unexpected gh command: {args}"
        raise AssertionError(msg)

    def _api(self, args: list[str]) -> tuple[str, str, int]:
        """Answer one REST call."""
        endpoint = args[1]
        if endpoint == f"repos/{REPOSITORY}/releases/tags/{fixtures.TAG}":
            if not self.exists:
                return ("", "gh: HTTP 404: Not Found", 1)
            return (self.document(), "", 0)
        if args[1:3] == ["--method", "POST"]:
            self.exists = True
            self.is_draft = True
            self.target = next(
                value.removeprefix("target_commitish=")
                for value in args
                if value.startswith("target_commitish=")
            )
            return ("{}", "", 0)
        if args[1:3] == ["--method", "PATCH"]:
            assert set(self.assets) == _release_names()
            self.is_draft = False
            self.published = True
            return ("{}", "", 0)
        msg = f"unexpected gh api call: {args}"
        raise AssertionError(msg)

    def _release(self, args: list[str]) -> tuple[str, str, int]:
        """Answer one release upload or download."""
        if args[1] == "upload":
            path = pathlib.Path(args[3])
            self.assets[path.name] = path.read_bytes()
            return ("", "", 0)
        if args[1] == "download":
            name = args[args.index("--pattern") + 1]
            directory = pathlib.Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
            return ("", "", 0)
        msg = f"unexpected gh release call: {args}"
        raise AssertionError(msg)


def _release_names() -> set[str]:
    """Return every asset name the release must serve."""
    return set(fixtures.names().values()) | set(EVIDENCE_NAMES)


def test_publish_drives_the_release_through_the_real_adapter(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole flow publishes the exact measured bytes and reports success."""
    _prepare(tmp_path)
    github = FakeGitHub()
    cmd_mox.stub("gh").runs(github.handle)
    code = main([
        "publish",
        "--manifest",
        str(tmp_path / "release" / "manifest.json"),
        "--receipt",
        str(tmp_path / "receipt.json"),
        "--payloads",
        str(tmp_path / "release"),
        *_run_flags(),
    ])
    assert code == 0
    assert github.published
    assert not github.is_draft
    assert github.target == fixtures.BUILDER_COMMIT
    expected = {
        path.name: path.read_bytes() for path in (tmp_path / "release").iterdir()
    }
    assert github.assets == expected
    assert fixtures.TAG in capsys.readouterr().out


def test_the_parser_requires_a_subcommand() -> None:
    """Running the module with no operation is a usage error, not a no-op."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
