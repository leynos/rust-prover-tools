"""Key-set and name policy for untrusted snapshot documents.

Both checks decide what a document is allowed to *name*: an object may not
introduce keys outside its schema, and a release asset may not name a path.
Keeping them together separates policy from the type validators in
`validation`.
"""

from __future__ import annotations

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "is_safe_asset_name",
    "reject_unknown_keys",
)


def is_safe_asset_name(name: str) -> bool:
    """Return whether a name is a plain basename that cannot escape a directory.

    Parameters
    ----------
    name : str
        Candidate release asset name.

    Returns
    -------
    bool
        `True` when the name contains no path separator, no whitespace or
        control characters, and is not a dot entry. Consumers build local
        paths from this name, so anything else could redirect a write or a
        read outside the asset directory. Whitespace is also refused because
        the checksum format splits rows on it.
    """
    if name in {"", ".", ".."}:
        return False
    if not name.isprintable() or any(char.isspace() for char in name):
        return False
    return "/" not in name and "\\" not in name


def reject_unknown_keys(
    document: dict[str, object],
    allowed: frozenset[str],
    field: str,
) -> None:
    """Reject every key outside `allowed`.

    Parameters
    ----------
    document : dict[str, object]
        Parsed object.
    allowed : frozenset[str]
        Exact permitted key set.
    field : str
        Object name used in the error message.

    Raises
    ------
    ProverToolError
        Raised when an unknown key is present.
    """
    unknown = sorted(set(document) - allowed)
    if unknown:
        msg = f"snapshot {field} has unknown keys: {', '.join(unknown)}"
        raise ProverToolError(msg)
