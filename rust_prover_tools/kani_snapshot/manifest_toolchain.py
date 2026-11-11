"""Strict parsing of the manifest's toolchain identity group.

Component archives are checked as published artefacts: an absolute HTTPS URL,
a positive length and a full digest. Extracted files must name a contained
relative path, so a recorded digest can never describe a file outside the
toolchain the installer verifies.
"""

from __future__ import annotations

import urllib.parse

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.toolchain_model import (
    ComponentRecord,
    FileRecord,
    ToolchainIdentity,
)
from rust_prover_tools.kani_snapshot.toolchain_name import is_safe_toolchain_name
from rust_prover_tools.kani_snapshot.validation import (
    is_contained_relative_path,
    require_mapping,
    require_positive_int,
    require_sequence,
    require_sha256,
    require_truthy_text,
)

__all__ = ("parse_toolchain",)

_TOOLCHAIN_KEYS: frozenset[str] = frozenset({
    "identifier",
    "rustc_vv",
    "kani_compiler",
    "channel_metadata_sha256",
    "components",
    "rustc_sha256",
    "rustup_components_sha256",
    "libraries",
})
_COMPONENT_KEYS: frozenset[str] = frozenset({
    "name",
    "version",
    "url",
    "size",
    "sha256",
})
_FILE_RECORD_KEYS: frozenset[str] = frozenset({"path", "sha256"})


def _require_https_url(value: object, field: str) -> str:
    """Require an absolute HTTPS URL on a rustup distribution host."""
    text = require_truthy_text(value, field)
    split = urllib.parse.urlsplit(text)
    if split.scheme != "https" or not split.hostname:
        msg = f"{field} must be an absolute HTTPS URL"
        raise ProverToolError(msg)
    return text


def _parse_component(document: dict[str, object], field: str) -> ComponentRecord:
    """Parse one Rustup component record."""
    reject_unknown_keys(document, _COMPONENT_KEYS, field)
    return ComponentRecord(
        name=require_truthy_text(document.get("name"), f"{field} name"),
        version=require_truthy_text(document.get("version"), f"{field} version"),
        url=_require_https_url(document.get("url"), f"{field} url"),
        size=require_positive_int(document.get("size"), f"{field} size"),
        sha256=require_sha256(document.get("sha256"), f"{field} sha256"),
    )


def _parse_file_record(document: dict[str, object], field: str) -> FileRecord:
    """Parse one extracted toolchain file record."""
    reject_unknown_keys(document, _FILE_RECORD_KEYS, field)
    path = require_truthy_text(document.get("path"), f"{field} path")
    if not is_contained_relative_path(path):
        msg = f"{field} path must be a contained relative path"
        raise ProverToolError(msg)
    return FileRecord(
        path=path,
        sha256=require_sha256(document.get("sha256"), f"{field} sha256"),
    )


def parse_toolchain(document: dict[str, object], field: str) -> ToolchainIdentity:
    """Parse the toolchain identity group.

    Parameters
    ----------
    document : dict[str, object]
        Untrusted group object.
    field : str
        Group name used in error messages.

    Returns
    -------
    ToolchainIdentity
        The validated toolchain identity.
    """
    reject_unknown_keys(document, _TOOLCHAIN_KEYS, field)
    components = tuple(
        _parse_component(
            require_mapping(item, f"{field} components item"),
            f"{field} components item",
        )
        for item in require_sequence(document.get("components"), f"{field} components")
    )
    libraries = tuple(
        _parse_file_record(
            require_mapping(item, f"{field} libraries item"),
            f"{field} libraries item",
        )
        for item in require_sequence(document.get("libraries"), f"{field} libraries")
    )
    identifier = require_truthy_text(document.get("identifier"), f"{field} identifier")
    if not is_safe_toolchain_name(identifier):
        msg = (
            f"{field} identifier must be one plain toolchain name, "
            f"not a path: {identifier!r}"
        )
        raise ProverToolError(msg)
    return ToolchainIdentity(
        identifier=identifier,
        rustc_vv=require_truthy_text(document.get("rustc_vv"), f"{field} rustc_vv"),
        kani_compiler=require_truthy_text(
            document.get("kani_compiler"), f"{field} kani_compiler"
        ),
        channel_metadata_sha256=require_sha256(
            document.get("channel_metadata_sha256"),
            f"{field} channel_metadata_sha256",
        ),
        components=components,
        rustc_sha256=require_sha256(
            document.get("rustc_sha256"), f"{field} rustc_sha256"
        ),
        rustup_components_sha256=require_sha256(
            document.get("rustup_components_sha256"),
            f"{field} rustup_components_sha256",
        ),
        libraries=libraries,
    )
