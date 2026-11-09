"""Models for the exact Rust toolchain and compiler a snapshot installs.

These records are the manifest's evidence about compiler bytes. Recording the
published Rustup archives and the digests of the extracted files keeps the
reviewed compiler an artefact of content rather than of a version string.
"""

from __future__ import annotations

import dataclasses as dc

__all__ = ("ComponentRecord", "FileRecord", "ToolchainIdentity")


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
        Lowercase SHA-256 of the toolchain's `lib/rustlib/components` list.
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
