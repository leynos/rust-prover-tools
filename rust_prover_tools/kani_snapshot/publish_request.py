"""The record of what one publication attempt is asked to publish.

Publication authenticates the release it finds; it must also refuse to upload
bytes the manifest does not describe. The request carries that intent — the
identity to publish, the validation evidence that authorises it, and where each
expected asset's bytes are meant to come from — so the rest of the attempt can
be decided without repeating it.

This module is a record only. Reading and hashing the local files is an
orchestration concern: `publish` checks them against the manifest before any
release API call, which keeps this record free of path and hashing concerns.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.release_policy import (
        ExpectedRelease,
        ValidationBinding,
    )

__all__ = ("PublicationRequest",)


@dc.dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Everything one publication attempt needs.

    Attributes
    ----------
    expected : ExpectedRelease
        Immutable identity to publish.
    validation : ValidationBinding
        Same-run clean-validation evidence.
    asset_paths : collections.abc.Mapping of str to Path
        Local file for each expected asset name.
    """

    expected: ExpectedRelease
    validation: ValidationBinding
    asset_paths: cabc.Mapping[str, Path]
