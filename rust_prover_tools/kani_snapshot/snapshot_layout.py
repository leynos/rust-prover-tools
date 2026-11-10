"""Absolute cache, generation and runtime paths for snapshot installation.

A deterministic cache key names one identity directory; each validated
installation is a uniquely named generation inside it. Setup writes only
generation-private homes, so two identities never share a Kani installation or
toolchain, even when upstream reports the same semantic version.
"""

from __future__ import annotations

import dataclasses as dc
import os
import secrets
import time
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.cache_key import identity_key

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "SnapshotLayout",
    "SnapshotRuntime",
    "identity_directory",
    "is_safe_generation_name",
    "new_generation_name",
    "runtime_for",
    "snapshot_layout",
)

#: Name prefix of a marker naming one unconfirmed payload publication.
PUBLICATION_MARKER_PREFIX: typ.Final[str] = ".publication-pending-"

_GENERATION_ALPHABET: typ.Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


@dc.dataclass(frozen=True, slots=True)
class SnapshotLayout:
    """Absolute locations for one identity and one generation.

    Attributes
    ----------
    identity_dir : Path
        Per-identity directory named by the deterministic cache key.
    generation_dir : Path
        Unique directory for one validated installation attempt.
    """

    identity_dir: Path
    generation_dir: Path

    @property
    def kani_home(self) -> Path:
        """Generation-private `KANI_HOME`."""
        return self.generation_dir / "kani-home"

    @property
    def rustup_home(self) -> Path:
        """Generation-private `RUSTUP_HOME`."""
        return self.generation_dir / "rustup-home"

    @property
    def cargo_home(self) -> Path:
        """Generation-private `CARGO_HOME`."""
        return self.generation_dir / "cargo-home"

    @property
    def launcher_dir(self) -> Path:
        """Staging root for the packaged launcher before setup."""
        return self.generation_dir / "launcher"

    @property
    def receipt_path(self) -> Path:
        """Generation-local setup receipt."""
        return self.generation_dir / "receipt.json"

    @property
    def python_root_record_path(self) -> Path:
        """Generation-local record of the setup-created Python root."""
        return self.generation_dir / "pyroot.json"

    @property
    def activations_dir(self) -> Path:
        """Identity-level directory of ordered activation records.

        The active generation is the highest-numbered record. Records are
        only ever created, never replaced, so an activation that was
        abandoned and later completes cannot overwrite a newer one.
        """
        return self.identity_dir / "active"

    @property
    def lock_path(self) -> Path:
        """Identity-level advisory installation lock."""
        return self.identity_dir / "install.lock"

    @property
    def downloads_dir(self) -> Path:
        """Identity-level verified-asset directory."""
        return self.identity_dir / "downloads"

    def publication_marker(self, publication_id: str) -> Path:
        """Return the marker naming one unconfirmed payload publication.

        Each publication owns its marker, so a publication that resumes after
        being abandoned can only remove its own marker. Readers treat any
        marker as an unfinished publication, and the next install clears the
        markers of publications that can no longer be running.
        """
        return self.identity_dir / f"{PUBLICATION_MARKER_PREFIX}{publication_id}"

    def publication_markers(self) -> tuple[Path, ...]:
        """Return every marker of an unconfirmed payload publication."""
        return tuple(self.identity_dir.glob(f"{PUBLICATION_MARKER_PREFIX}*"))


@dc.dataclass(frozen=True, slots=True)
class SnapshotRuntime:
    """Resolved generation paths for one exact snapshot.

    Attributes
    ----------
    layout : SnapshotLayout
        Layout the paths were derived from.
    toolchain : str
        Rust toolchain identifier installed by setup.
    backend_version : str
        Upstream Kani package version, preserved unchanged.
    """

    layout: SnapshotLayout
    toolchain: str
    backend_version: str

    @property
    def kani_dir(self) -> Path:
        """Setup-created `kani-<VERSION>` directory."""
        return self.layout.kani_home / f"kani-{self.backend_version}"

    @property
    def launcher(self) -> Path:
        """Absolute `kani` launcher."""
        return self.kani_dir / "bin" / "kani"

    @property
    def driver(self) -> Path:
        """Absolute `kani-driver` executable."""
        return self.kani_dir / "bin" / "kani-driver"

    @property
    def compiler(self) -> Path:
        """Absolute `kani-compiler` executable."""
        return self.kani_dir / "bin" / "kani-compiler"

    @property
    def rustc(self) -> Path:
        """Exact `rustc` installed for the pinned toolchain."""
        return self.layout.rustup_home / "toolchains" / self.toolchain / "bin/rustc"

    @property
    def python_root(self) -> Path:
        """Bundled Python support tree."""
        return self.kani_dir / "pyroot"


def identity_directory(pin: SnapshotPin, *, cache_home: Path | None = None) -> Path:
    """Return the absolute per-identity cache directory for a pin.

    Parameters
    ----------
    pin : SnapshotPin
        Validated pin.
    cache_home : Path | None
        Explicit cache root. Defaults to `XDG_CACHE_HOME`, then `~/.cache`.

    Returns
    -------
    Path
        Absolute directory named by the deterministic identity key.
    """
    root = cache_home if cache_home is not None else _default_cache_home()
    return root.expanduser().resolve() / identity_key(pin)


def _default_cache_home() -> Path:
    """Return the user's cache root without creating it."""
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".cache"


def new_generation_name() -> str:
    """Return a unique, filesystem-safe generation name."""
    return f"{time.time_ns():x}-{os.getpid():x}-{secrets.token_hex(8)}"


def is_safe_generation_name(name: str) -> bool:
    """Return whether `name` is one plain, non-special path component."""
    if not name or name in {".", ".."}:
        return False
    if not name.isprintable():
        return False
    return all(char in _GENERATION_ALPHABET for char in name)


def snapshot_layout(
    pin: SnapshotPin,
    *,
    cache_home: Path | None = None,
    generation: str | None = None,
) -> SnapshotLayout:
    """Return the layout for a new or named generation.

    Parameters
    ----------
    pin : SnapshotPin
        Validated pin.
    cache_home : Path | None
        Explicit cache root.
    generation : str | None
        Existing generation name, or a fresh unique name when omitted.

    Returns
    -------
    SnapshotLayout
        Absolute identity and generation directories.

    Raises
    ------
    ProverToolError
        Raised when a supplied generation name is not one safe component.
    """
    name = new_generation_name() if generation is None else generation
    if not is_safe_generation_name(name):
        msg = f"snapshot generation name is not one safe component: {name!r}"
        raise ProverToolError(msg)
    identity_dir = identity_directory(pin, cache_home=cache_home)
    return SnapshotLayout(
        identity_dir=identity_dir,
        generation_dir=identity_dir / "generations" / name,
    )


def runtime_for(layout: SnapshotLayout, manifest: SnapshotManifest) -> SnapshotRuntime:
    """Derive the exact generation paths from a layout and manifest."""
    return SnapshotRuntime(
        layout=layout,
        toolchain=manifest.toolchain.identifier,
        backend_version=manifest.backend.version,
    )
