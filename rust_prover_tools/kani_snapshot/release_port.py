"""The release port publication needs from a GitHub adapter.

`publish` owns the orchestration and `gh_release_api` owns the commands; this
module owns the contract between them. Keeping the port separate means the
orchestration can be read without also reading every operation's contract, and
an adapter can be written against the port alone.

The port separates reading state from verifying bytes: `observe` performs
queries only, and `verify` is the one command that downloads and authenticates.
A caller can therefore tell whether an observation cost a download, and the
policy can ask for verification explicitly rather than receiving hashes that
may or may not have been computed.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.release_policy import (
        ExpectedRelease,
        ObservedAsset,
        ObservedRelease,
        ReleaseAsset,
        VerifiedAssets,
    )

__all__ = ("ReleaseApi",)


class ReleaseApi(typ.Protocol):
    """The release operations publication needs from an adapter.

    The port separates reading state from verifying bytes: `observe` performs
    queries only, and `verify` is the one command that downloads and
    authenticates. A caller can therefore tell whether an observation cost a
    download, and the policy can ask for verification explicitly rather than
    receiving hashes that may or may not have been computed.
    """

    def observe(self, tag: str) -> ObservedRelease:
        """Return the current release metadata, without downloading assets.

        Parameters
        ----------
        tag : str
            Immutable snapshot tag to look up.

        Returns
        -------
        ObservedRelease
            The current state. An absent release is `exists=False`. Any asset
            digest in the result is what the API volunteered, not a computed
            one, and `provenance_verified` is always `False`: only `verify`
            may claim that a release's bytes were authenticated.

        Raises
        ------
        ProverToolError
            Raised when the state cannot be read, or when it describes a
            release this run must refuse rather than act on.
        ReleaseTransientError
            Raised when the read failed in a way a later attempt may survive.
        """
        ...

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Download the named assets, hash them and authenticate the evidence.

        Parameters
        ----------
        tag : str
            Release to read.
        observed : ObservedRelease
            Observation the result is bound to. The release identity, body and
            asset set come from it, so a result can never describe another
            release than the one the caller decided about.
        names : collections.abc.Sequence[str]
            Asset names the caller intends to rely on. The adapter refuses a
            name the observation does not report.

        Returns
        -------
        VerifiedAssets
            Digests computed from the bytes the release serves at verification
            time, beside the attestation verdict for those same bytes. A digest
            here is evidence about content, not a confirmation of the metadata
            in `observed`.

        Raises
        ------
        ReleaseTransientError
            Raised when the bytes could not be fetched or a verifier timed
            out. The caller must observe again rather than treat the release
            as invalid.
        ProverToolError
            Raised for an unknown asset name or a permanent failure.
        """
        ...

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create a draft release at the expected builder commit.

        Parameters
        ----------
        expected : ExpectedRelease
            Identity to create, including the run marker and build-input
            fingerprint recorded in the release body.

        Raises
        ------
        ReleaseTransientError
            Raised when the create call failed after it may have taken effect,
            so the caller must observe the tag instead of assuming an outcome.
        """
        ...

    def discard(self, tag: str, asset: ObservedAsset) -> None:
        """Delete one asset record that holds no uploaded bytes.

        Parameters
        ----------
        tag : str
            Draft that lists the record.
        asset : ObservedAsset
            Placeholder record, including the server identifier the delete
            addresses.

        Raises
        ------
        ReleaseTransientError
            Raised when the delete failed after it may have taken effect;
            re-observing shows whether the name is now free.
        ProverToolError
            Raised when the record carries no identifier to delete.
        """
        ...

    def upload(self, tag: str, asset: ReleaseAsset, path: Path) -> None:
        """Upload one missing asset without overwriting anything.

        Parameters
        ----------
        tag : str
            Draft to upload into.
        asset : ReleaseAsset
            Expected asset identity the local file must match.
        path : Path
            Local file holding the exact bytes.

        Raises
        ------
        ReleaseTransientError
            Raised when the transfer failed after the bytes may have been
            stored, so the caller must observe the draft again.
        """
        ...

    def publish(self, tag: str, release_id: int) -> None:
        """Make a complete, verified draft public.

        Parameters
        ----------
        tag : str
            Draft tag being published.
        release_id : int
            Identifier of the draft the fresh observation reported.

        Raises
        ------
        ReleaseTransientError
            Raised when the publish call failed after the PATCH may have
            succeeded; the caller observes the release again and decides from
            what it now reports.
        """
        ...
