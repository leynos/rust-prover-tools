"""Strict parsing of reviewed Kani snapshot pins.

A pin is a small TOML document checked into a consumer repository. It names
one immutable snapshot and the exact manifest digest a reviewer approved. The
parser accepts only the version 1 key set, rejects coercion, and requires the
tag's upstream prefix to agree with the pinned commit.
"""

from __future__ import annotations

import tomllib
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION, SnapshotPin
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    reject_unknown_keys,
    require_full_sha,
    require_int,
    require_sha256,
    validate_repository,
    validate_target,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "PIN_KEYS",
    "load_pin",
    "parse_pin",
    "pin_to_toml",
)

#: The exact key set accepted in schema version 1.
PIN_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "repository",
    "tag",
    "upstream_commit",
    "builder_commit",
    "target",
    "manifest_sha256",
})


def _decode(data: bytes) -> dict[str, object]:
    """Decode a TOML pin, refusing invalid UTF-8 and TOML syntax."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"snapshot pin is not valid UTF-8: {exc}"
        raise ProverToolError(msg) from exc
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"snapshot pin is not valid TOML: {exc}"
        raise ProverToolError(msg) from exc
    return typ.cast("dict[str, object]", document)


def _check_schema_version(value: object) -> int:
    """Require the single supported schema version."""
    version = require_int(value, "pin schema_version")
    if version != SCHEMA_VERSION:
        msg = f"snapshot pin schema_version must be {SCHEMA_VERSION}, got {version}"
        raise ProverToolError(msg)
    return version


def parse_pin(data: bytes) -> SnapshotPin:
    """Parse exact pin bytes into a validated `SnapshotPin`.

    Parameters
    ----------
    data : bytes
        Exact TOML bytes read from a reviewed pin.

    Returns
    -------
    SnapshotPin
        The validated pin.

    Raises
    ------
    ProverToolError
        Raised for invalid UTF-8, TOML syntax, duplicate or unknown keys, wrong
        types, malformed identities, an unsupported target, or a tag prefix that
        disagrees with the upstream commit.
    """
    document = _decode(data)
    reject_unknown_keys(document, PIN_KEYS, "pin")
    missing = sorted(PIN_KEYS - set(document))
    if missing:
        msg = f"snapshot pin is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    upstream = require_full_sha(document["upstream_commit"], "pin upstream_commit")
    return SnapshotPin(
        schema_version=_check_schema_version(document["schema_version"]),
        repository=validate_repository(document["repository"]),
        tag=validate_tag(document["tag"], upstream),
        upstream_commit=upstream,
        builder_commit=require_full_sha(
            document["builder_commit"], "pin builder_commit"
        ),
        target=validate_target(document["target"]),
        manifest_sha256=require_sha256(
            document["manifest_sha256"], "pin manifest_sha256"
        ),
    )


def load_pin(path: Path) -> SnapshotPin:
    """Read and parse a pin file.

    Parameters
    ----------
    path : Path
        Pin file to read.

    Returns
    -------
    SnapshotPin
        The validated pin.

    Raises
    ------
    ProverToolError
        Raised when the file cannot be read or fails `parse_pin`.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"failed to read snapshot pin '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return parse_pin(data)


def _quoted(value: str) -> str:
    """Quote a TOML basic string without permitting an escape injection."""
    if any(character in value for character in ('"', "\\", "\n", "\r", "\x00")):
        msg = "snapshot pin value cannot be rendered as TOML"
        raise ProverToolError(msg)
    return f'"{value}"'


def pin_to_toml(pin: SnapshotPin) -> bytes:
    """Render a validated pin as deterministic TOML bytes.

    Parameters
    ----------
    pin : SnapshotPin
        Pin to render.

    Returns
    -------
    bytes
        UTF-8 TOML ending in one newline.
    """
    lines = (
        f"schema_version = {pin.schema_version}",
        f"repository = {_quoted(pin.repository)}",
        f"tag = {_quoted(pin.tag)}",
        f"upstream_commit = {_quoted(pin.upstream_commit)}",
        f"builder_commit = {_quoted(pin.builder_commit)}",
        f"target = {_quoted(pin.target)}",
        f"manifest_sha256 = {_quoted(pin.manifest_sha256)}",
    )
    return ("\n".join(lines) + "\n").encode("utf-8")
