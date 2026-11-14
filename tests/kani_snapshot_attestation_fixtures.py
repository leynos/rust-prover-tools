"""A modelled `gh attestation verify` shared by the snapshot authentication tests.

`gh attestation verify` accepts a bundle only when the certificate and
statement inside it satisfy every policy flag the command supplies. The double
here models that comparison instead of returning a fixed answer: it holds the
identity a modelled bundle claims, reads the trusted flags from the argument
vector the production code built, refuses the first dimension where the two
disagree, and accepts a bundle whose identity matches. A test can therefore
show that a matching bundle passes, that a wrong signer workflow, source ref,
source digest or signer digest is refused, and that the refusal is surfaced
rather than masked.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cmd_mox import Invocation

    from rust_prover_tools.kani_snapshot.attestation import AttestationPolicy
    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

#: Policy flags `gh attestation verify` compares with the bundle, with the
#: policy field each one carries.
POLICY_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("--signer-workflow", "signer_workflow"),
    ("--source-ref", "source_ref"),
    ("--source-digest", "source_digest"),
    ("--signer-digest", "signer_digest"),
)

#: The refusal `gh` reports when a bundle cannot satisfy one policy dimension.
REFUSALS: dict[str, str] = {
    "signer_workflow": "the certificate's workflow is not trusted",
    "source_ref": "the statement's source ref is not trusted",
    "source_digest": "the statement's source digest is not trusted",
    "signer_digest": "the certificate's signer digest is not trusted",
}


def flag_value(args: cabc.Sequence[str], flag: str) -> str | None:
    """Return the value following `flag`, or `None` when it is absent.

    The two failure modes are kept apart rather than caught as one tuple: the
    hosted `interrogate` tool parses this tree under Python 3.13, which cannot
    read the parenthesis-free multiple-exception form that `ruff format`
    produces for the project's 3.14 target.
    """
    try:
        index = args.index(flag)
    except ValueError:
        return None
    following = index + 1
    return args[following] if following < len(args) else None


def _verified_output(invocation_id: str) -> str:
    """Return verifier output holding one verified record naming a run."""
    return json.dumps([
        {
            "attestation": {},
            "verificationResult": {
                "signature": {"certificate": {}},
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {
                        "runDetails": {"metadata": {"invocationId": invocation_id}}
                    },
                },
            },
        }
    ])


def _bundle_run_url(trusted: AttestationPolicy, workflow: WorkflowIdentity) -> str:
    """Return the run URL a bundle built by `trusted` would name."""
    return (
        f"https://github.com/{trusted.repository}"
        f"/actions/runs/{workflow.run_id}/attempts/{workflow.run_attempt}"
    )


def bundle_verifier(
    trusted: AttestationPolicy,
    *,
    workflow: WorkflowIdentity,
    bundle: AttestationPolicy | None = None,
) -> cabc.Callable[[Invocation], tuple[str, str, int]]:
    """Return a `gh` double comparing trusted flags with a modelled bundle.

    Parameters
    ----------
    trusted : AttestationPolicy
        Identity the command is expected to request. A flag naming any other
        value is refused, so a weakened policy cannot pass.
    workflow : WorkflowIdentity
        Run and attempt the accepted bundle's statement names.
    bundle : AttestationPolicy | None
        Identity the modelled bundle claims. Defaults to `trusted`, which is
        how a matching bundle is shown to be accepted rather than assumed.

    Returns
    -------
    cabc.Callable[[Invocation], tuple[str, str, int]]
        Handler answering like `gh attestation verify`.
    """

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Refuse the first dimension the flags and the bundle disagree on."""
        claimed = trusted if bundle is None else bundle
        for flag, attribute in POLICY_DIMENSIONS:
            requested = flag_value(invocation.args, flag)
            if requested is None:
                return ("", f"missing verifier flag: {flag}", 2)
            expected = getattr(trusted, attribute)
            if requested != expected or requested != getattr(claimed, attribute):
                return ("", f"no matching attestations: {REFUSALS[attribute]}", 1)
        return (_verified_output(_bundle_run_url(trusted, workflow)), "", 0)

    return handler
