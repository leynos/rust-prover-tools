"""Publication provenance is decided by the consumer's own verifier.

A published release authenticates itself only if its evidence, its recorded
identity and its attestation all agree, and if the attestation satisfies the
policy a consumer installs with. These tests serve a real assembled release and
then alter one dimension at a time, so a check that would pass on structural
well-formedness alone is shown to refuse the release instead.
"""

from __future__ import annotations

import base64
import dataclasses as dc
import json
import pathlib
import typing as typ

import pytest

from rust_prover_tools.commands import CommandTimeoutError
from rust_prover_tools.kani_snapshot.attestation import (
    ATTESTATION_PREDICATE,
    SIGNER_WORKFLOW_PATH,
    AttestationPolicy,
)
from rust_prover_tools.kani_snapshot.gh_release.api import GhReleaseApi
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.orchestrator.assembly import MANIFEST_NAME
from rust_prover_tools.kani_snapshot.orchestrator.release_evidence import (
    ATTESTATION_NAME,
    CHECKSUMS_NAME,
)
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.release_model import (
    ReleaseTransientError,
    VerifiedAssets,
    release_body,
)
from rust_prover_tools.unittests import kani_producer_fixtures as producer
from tests import kani_publish_fixtures as fixtures
from tests.kani_snapshot_attestation_fixtures import bundle_verifier

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation

    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity


pytest_plugins = ("cmd_mox.pytest_plugin",)

#: A commit the immutable tag addresses, distinct from release metadata.
TAG_COMMIT: typ.Final[str] = "a" * 40

#: The failure `run_checked` reports when the verifier outlives its budget.
_VERIFIER_TIMEOUT_MESSAGE: typ.Final[str] = (
    "command timed out after 300 seconds: gh attestation verify"
)


def _object(kind: str, sha: str) -> str:
    """Return a Git object reference as `--jq .object` prints it."""
    return json.dumps({"type": kind, "sha": sha})


def _fingerprint() -> str:
    """Return the build-input fingerprint the assembled fixture records."""
    return input_fingerprint(
        upstream_commit=producer.UPSTREAM_COMMIT,
        builder_commit=producer.BUILDER_COMMIT,
        recipe=pinned_recipe(),
    )


def _trusted_policy() -> AttestationPolicy:
    """Return the identity policy the manifest fixture requires."""
    return AttestationPolicy(
        repository=REPOSITORY,
        signer_workflow=f"{REPOSITORY}/{SIGNER_WORKFLOW_PATH}",
        source_digest=producer.BUILDER_COMMIT,
        signer_digest=producer.BUILDER_COMMIT,
    )


class _ServedRelease:
    """A published release `gh` answers with, serving its exact bytes.

    Every read the adapter performs is answered from one directory, so the
    digests and the publication checks are computed from what the release
    really serves rather than from a claim in the document. `gh attestation
    verify` is answered by the modelled verifier, so a test can show that a
    bundle the pinned policy rejects leaves the release unverified.
    """

    def __init__(
        self,
        directory: pathlib.Path,
        body_text: str | None,
        *,
        claimed: AttestationPolicy | None = None,
        run: WorkflowIdentity | None = None,
    ) -> None:
        """Record the served directory, body and the identity the bundle claims."""
        self._directory = directory
        self._body = body_text
        self._names = sorted(path.name for path in directory.iterdir())
        self._verifier = bundle_verifier(
            _trusted_policy(),
            workflow=producer.workflow() if run is None else run,
            bundle=claimed,
        )

    def document(self) -> str:
        """Return the release as `gh api --jq` would print it."""
        return json.dumps({
            "id": 4242,
            "tag_name": producer.TAG,
            "draft": False,
            "target_commitish": producer.BUILDER_COMMIT,
            "body": self._body,
            "assets": [
                {
                    "name": name,
                    "size": (self._directory / name).stat().st_size,
                    "digest": f"sha256:{fixtures.digest(self._data(name))}",
                }
                for name in self._names
            ],
        })

    def _data(self, name: str) -> bytes:
        """Return one served file's exact bytes."""
        return (self._directory / name).read_bytes()

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh` invocation from the served release."""
        args = list(invocation.args)
        if args[:2] == ["attestation", "verify"]:
            return self._verifier(invocation)
        if args[:2] == ["api", f"repos/{REPOSITORY}/releases/tags/{producer.TAG}"]:
            return (self.document(), "", 0)
        if args[:2] == ["api", f"repos/{REPOSITORY}/git/ref/tags/{producer.TAG}"]:
            return (_object("commit", TAG_COMMIT), "", 0)
        if args[:2] == ["release", "download"]:
            name = args[args.index("--pattern") + 1]
            directory = pathlib.Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self._data(name))
            return ("", "", 0)
        msg = f"unexpected gh call: {args}"
        raise AssertionError(msg)


@dc.dataclass(frozen=True, slots=True)
class _Signature:
    """The identity a modelled attestation bundle claims, and the run it names."""

    claimed: AttestationPolicy | None = None
    run: WorkflowIdentity | None = None


def _served_release(
    tmp_path: pathlib.Path,
    *,
    attestation: bytes | None = None,
    fingerprint: str | None = None,
    signature: _Signature | None = None,
) -> _ServedRelease:
    """Serve a real assembled release, optionally with altered provenance."""
    directory = tmp_path / "served"
    assembly = producer.assemble_into(directory)
    (directory / MANIFEST_NAME).write_bytes(assembly.manifest_bytes)
    (directory / CHECKSUMS_NAME).write_bytes(assembly.checksums)
    (directory / ATTESTATION_NAME).write_bytes(
        producer.attestation_bundle(assembly.manifest_sha256)
        if attestation is None
        else attestation
    )
    body_text = release_body(
        producer.TAG,
        fingerprint or _fingerprint(),
        run_id=fixtures.RUN_ID,
        run_attempt=fixtures.RUN_ATTEMPT,
    )
    signed = signature or _Signature()
    return _ServedRelease(directory, body_text, claimed=signed.claimed, run=signed.run)


def _verify_served(api: GhReleaseApi, tag: str) -> VerifiedAssets:
    """Observe a served release and verify every asset it reports."""
    observed = api.observe(tag)
    names = [asset.name for asset in observed.assets]
    return api.verify(tag, observed, names)


def test_verify_authenticates_a_published_release_from_its_own_bytes(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A release whose evidence, identity and attestation agree is verified."""
    cmd_mox.stub("gh").runs(_served_release(tmp_path).handle)
    api = GhReleaseApi()
    observed = api.observe(producer.TAG)
    assert observed.target_commitish == TAG_COMMIT
    assert not observed.provenance_verified, "a state query authenticated a release"
    verified = _verify_served(api, producer.TAG)
    assert verified.provenance_verified
    assert verified.reason is None


@pytest.mark.parametrize(
    "attestation",
    [
        pytest.param(
            b'{"dsseEnvelope": {"payload": "", "signatures": []}}',
            id="empty-envelope",
        ),
        pytest.param(
            producer.attestation_bundle("e" * 64), id="statement-about-other-bytes"
        ),
    ],
)
def test_verify_reports_an_unrelated_attestation_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    attestation: bytes,
) -> None:
    """An attestation that does not name this manifest is not provenance."""
    cmd_mox.stub("gh").runs(_served_release(tmp_path, attestation=attestation).handle)
    verified = _verify_served(GhReleaseApi(), producer.TAG)
    assert not verified.provenance_verified
    assert verified.reason is not None


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("source_digest", id="foreign-source-digest"),
        pytest.param("signer_digest", id="foreign-signer-digest"),
    ],
)
def test_verify_reports_a_foreign_signature_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    field: str,
) -> None:
    """A bundle the pinned certificate policy rejects does not authenticate.

    Structural well-formedness is not provenance: the verifier compares the
    bundle's claimed workflow, ref and digests with the policy flags, so a
    bundle signed by anything else must leave the release unverified. Only the
    named digest differs from the trusted policy, so removing either restriction
    alone would let a rejected bundle through and fail this test.
    """
    claimed = dc.replace(_trusted_policy(), **{field: "f" * 40})
    signed = _Signature(claimed=claimed)
    cmd_mox.stub("gh").runs(_served_release(tmp_path, signature=signed).handle)
    assert not _verify_served(GhReleaseApi(), producer.TAG).provenance_verified


def test_verify_reports_a_signature_from_another_run_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A verified bundle for a different attempt authenticates another run."""
    signed = _Signature(run=producer.workflow(run_attempt=2))
    cmd_mox.stub("gh").runs(_served_release(tmp_path, signature=signed).handle)
    assert not _verify_served(GhReleaseApi(), producer.TAG).provenance_verified


def test_verify_reports_a_body_that_does_not_bind_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A body recording other build inputs does not authenticate the release."""
    served = _served_release(tmp_path, fingerprint=fixtures.OTHER_FINGERPRINT)
    cmd_mox.stub("gh").runs(served.handle)
    verified = _verify_served(GhReleaseApi(), producer.TAG)
    assert not verified.provenance_verified
    assert verified.reason == (
        "published manifest does not match the release's recorded build inputs"
    )


def _statement_bundle(statement: dict[str, object]) -> bytes:
    """Return a Sigstore bundle whose payload is `statement`.

    The signature is present but unchecked here because the structural check
    runs before any verifier process, which is what lets a malformed statement
    be shown to leave the release unverified rather than raise.
    """
    payload = base64.b64encode(json.dumps(statement).encode("utf-8")).decode("ascii")
    return json.dumps({
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "dsseEnvelope": {
            "payload": payload,
            "payloadType": "application/vnd.in-toto+json",
            "signatures": [{"sig": "c2ln"}],
        },
    }).encode("ascii")


def test_verify_reports_a_non_string_statement_type_as_unverified(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
) -> None:
    """A `_type` array must be refused, not raise an unhashable-type error."""
    statement: dict[str, object] = {
        "_type": [],
        "subject": [],
        "predicateType": ATTESTATION_PREDICATE,
        "predicate": {"buildDefinition": {}},
    }
    served = _served_release(
        tmp_path,
        attestation=_statement_bundle(statement),
    )
    cmd_mox.stub("gh").runs(served.handle)
    assert not _verify_served(GhReleaseApi(), producer.TAG).provenance_verified


def test_verify_propagates_a_verifier_timeout_as_transient(
    tmp_path: pathlib.Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out verifier is retryable, unlike a rejected signature.

    The verifier is an external process whose own budget can expire. That says
    nothing about the release, so the verification is transient rather than
    unverified; a genuinely invalid signature still reports a reason, which the
    neighbouring foreign-signature test pins.
    """
    cmd_mox.stub("gh").runs(_served_release(tmp_path).handle)

    def _timed_out(*args: object, **kwargs: object) -> None:
        """Fail as `run_checked` does when the verifier outlives its budget."""
        raise CommandTimeoutError(_VERIFIER_TIMEOUT_MESSAGE)

    # The attestation step re-imports the verifier by name, so the patch must
    # replace the binding in the module that calls it, not its origin.
    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.gh_release.attestation.verify_attestation",
        _timed_out,
    )

    with pytest.raises(ReleaseTransientError, match="attestation verification"):
        _verify_served(GhReleaseApi(), producer.TAG)
