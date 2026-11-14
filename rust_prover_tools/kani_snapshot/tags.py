"""Immutable snapshot tag grammar and its binding to an upstream commit.

Tags are immutable identities, not convenience labels: a changed input gets a
new positive revision rather than a moved tag. The embedded 12-hex prefix must
agree with the pinned upstream commit so a tag cannot name a different source
than the manifest it accompanies.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import require_text

__all__ = (
    "TAG_PATTERN",
    "tag_prefix",
    "validate_tag",
)

#: The only accepted tag shape, anchored by `fullmatch` at each call site.
TAG_PATTERN: typ.Final[re.Pattern[str]] = re.compile(
    r"kani-snapshot-(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])-"
    r"(?P<prefix>[0-9a-f]{12})-r(?P<revision>[1-9][0-9]*)",
)

#: Characters of the upstream commit that the tag must carry.
_PREFIX_LENGTH: typ.Final[int] = 12


def validate_tag(value: object, upstream_commit: str) -> str:
    """Return a tag in the immutable grammar, bound to its upstream commit.

    Parameters
    ----------
    value : object
        Untrusted tag.
    upstream_commit : str
        Already validated full upstream commit.

    Returns
    -------
    str
        The validated tag.

    Raises
    ------
    ProverToolError
        Raised when the tag grammar or its upstream prefix disagrees.
    """
    text = require_text(value, "tag")
    match = TAG_PATTERN.fullmatch(text)
    if match is None:
        msg = (
            "snapshot tag must match kani-snapshot-YYYY-MM-<12-hex-upstream-prefix>-rN"
        )
        raise ProverToolError(msg)
    if match.group("prefix") != upstream_commit[:_PREFIX_LENGTH]:
        msg = "snapshot tag prefix must match the pinned upstream commit"
        raise ProverToolError(msg)
    return text


def tag_prefix(tag: str) -> str:
    """Return the tag's 12-hex upstream prefix.

    Parameters
    ----------
    tag : str
        Already validated tag.

    Returns
    -------
    str
        The upstream commit prefix embedded in the tag.
    """
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:  # pragma: no cover - callers pass a validated tag
        msg = "snapshot tag is not in the immutable grammar"
        raise ProverToolError(msg)
    return match.group("prefix")
