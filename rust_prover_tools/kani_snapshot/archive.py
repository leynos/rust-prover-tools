"""Contained, bounded reading and extraction of snapshot tar archives.

The behaviour is split by concern:

- `archive_graph` defines the path grammar, policy and link-graph rules;
- `archive_read` validates every effective member header;
- `archive_extract` materializes a validated tree and publishes it atomically.

Callers use this module's `read_archive` and `extract_archive` facade.
"""

from __future__ import annotations

from rust_prover_tools.kani_snapshot.archive_extract import extract_archive
from rust_prover_tools.kani_snapshot.archive_graph import (
    DEFAULT_ARCHIVE_LIMITS,
    DEFAULT_ARCHIVE_POLICY,
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    ArchiveEntry,
    ArchiveLimits,
    ArchivePolicy,
    normalize_member_path,
)
from rust_prover_tools.kani_snapshot.archive_read import read_archive

__all__ = (
    "DEFAULT_ARCHIVE_LIMITS",
    "DEFAULT_ARCHIVE_POLICY",
    "KIND_DIRECTORY",
    "KIND_FILE",
    "KIND_HARDLINK",
    "KIND_SYMLINK",
    "ArchiveEntry",
    "ArchiveLimits",
    "ArchivePolicy",
    "extract_archive",
    "normalize_member_path",
    "read_archive",
)
