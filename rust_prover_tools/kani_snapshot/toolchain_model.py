"""Models for the exact Rust toolchain and compiler a snapshot installs.

These records are the manifest's evidence about compiler bytes. Recording the
published Rustup archives and the digests of the extracted files keeps the
reviewed compiler an artefact of content rather than of a version string.

Everything here is pure: the byte ceiling and the canonicalization are domain
rules, so they can be tested from literal bytes. The adapter that reads a
component record from disk is `components_file`, which both the producer's
measurement and the consumer's check call.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "MAX_COMPONENTS_BYTES",
    "ComponentRecord",
    "FileRecord",
    "ToolchainIdentity",
    "canonical_component_names",
    "components_digest",
)

#: Largest rustup component record accepted. Rustup writes a few short lines.
MAX_COMPONENTS_BYTES: typ.Final[int] = 64 * 1024


def canonical_component_names(data: bytes) -> bytes:
    """Return the canonical form of a rustup component record.

    Rustup writes `<toolchain>/lib/rustlib/components` in component-completion
    order, which is not deterministic even for identical installs. The
    meaningful content is the set of installed component names, so the
    canonical form is those names sorted, one per line, ending in a newline.
    Ordering, trailing whitespace, CRLF and blank lines therefore do not alter
    the digest, while an added, removed or renamed component does.

    Parameters
    ----------
    data : bytes
        Exact bytes of the components file.

    Returns
    -------
    bytes
        UTF-8 canonical name list.

    Raises
    ------
    ProverToolError
        Raised when the record is larger than `MAX_COMPONENTS_BYTES`, is not
        UTF-8, lists no components, or lists one component more than once.
    """
    names = _component_names(_decode_components(data))
    return "".join(f"{name}\n" for name in sorted(names)).encode("utf-8")


def _decode_components(data: bytes) -> str:
    """Decode a bounded component record as UTF-8."""
    if len(data) > MAX_COMPONENTS_BYTES:
        msg = f"rustup component record exceeds the {MAX_COMPONENTS_BYTES}-byte ceiling"
        raise ProverToolError(msg)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"rustup component record is not valid UTF-8: {exc}"
        raise ProverToolError(msg) from exc


def _component_names(text: str) -> list[str]:
    """Return the non-blank names, requiring a unique non-empty set."""
    names = [line.strip() for line in text.splitlines() if line.strip()]
    if not names:
        msg = "rustup component record lists no components"
        raise ProverToolError(msg)
    if len(set(names)) != len(names):
        msg = "rustup component record lists a component more than once"
        raise ProverToolError(msg)
    return names


def components_digest(data: bytes) -> str:
    """Return the digest of a canonical rustup component record.

    Parameters
    ----------
    data : bytes
        Exact bytes of the components file.

    Returns
    -------
    str
        Lowercase SHA-256 of `canonical_component_names(data)`.

    Raises
    ------
    ProverToolError
        Raised when the record is not a valid component list.
    """
    return hashlib.sha256(canonical_component_names(data)).hexdigest()


@dc.dataclass(frozen=True, slots=True)
class ComponentRecord:
    """One Rustup component archive the producer installed.

    Recording the published archive's URL, length and digest binds the
    installed compiler to a distributable artefact a reviewer can re-fetch.

    Attributes
    ----------
    name : str
        Rustup component name, such as `rustc-dev`.
    version : str
        Component version from the channel metadata.
    url : str
        Official HTTPS archive URL.
    size : int
        Exact compressed archive length in bytes.
    sha256 : str
        Lowercase SHA-256 of the compressed archive.
    """

    name: str
    version: str
    url: str
    size: int
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class FileRecord:
    """One extracted toolchain file and its digest.

    Attributes
    ----------
    path : str
        Path relative to the toolchain root, without traversal.
    sha256 : str
        Lowercase SHA-256 of the file's exact bytes.
    """

    path: str
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class ToolchainIdentity:
    """The exact Rust and Kani compiler installed by setup.

    Attributes
    ----------
    identifier : str
        Rust toolchain identifier recorded by the bundle.
    rustc_vv : str
        Exact `rustc -vV` identity expected from the effective compiler.
    kani_compiler : str
        Exact identity reported by the bundled `kani-compiler`.
    channel_metadata_sha256 : str
        Lowercase SHA-256 of the Rustup channel metadata the producer used.
    components : tuple[ComponentRecord, ...]
        Rustup component archives installed for this toolchain.
    rustc_sha256 : str
        Lowercase SHA-256 of the extracted `rustc` executable.
    rustup_components_sha256 : str
        Lowercase SHA-256 of the **canonical** `lib/rustlib/components`
        record: its sorted component names, one per line. Rustup writes the
        file in nondeterministic completion order, so a digest of its raw
        bytes cannot be stable across two installs.
    libraries : tuple[FileRecord, ...]
        Extracted compiler and runtime libraries, relative to the toolchain.
    """

    identifier: str
    rustc_vv: str
    kani_compiler: str
    channel_metadata_sha256: str = ""
    components: tuple[ComponentRecord, ...] = ()
    rustc_sha256: str = ""
    rustup_components_sha256: str = ""
    libraries: tuple[FileRecord, ...] = ()

    def __post_init__(self) -> None:
        """Require the library evidence every toolchain identity must carry.

        An empty list would leave every installed shared library unverified,
        and a repeated path would make one digest ambiguous. Both are domain
        invariants, so they hold for any constructed identity rather than only
        for one parsed from manifest bytes.
        """
        if not self.libraries:
            msg = "toolchain libraries must record at least one extracted library"
            raise ProverToolError(msg)
        paths = [library.path for library in self.libraries]
        if len(set(paths)) != len(paths):
            msg = "toolchain libraries records a path more than once"
            raise ProverToolError(msg)
