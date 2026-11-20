"""An archive's own headers drive the inventory a consumer validates.

A hard-link group is indistinguishable on disk: every name is a regular file
sharing one inode, so a tree walk records whichever name it meets first as the
carrier. The archive that produced the tree is the only authority on which name
carried the bytes. These tests build a real archive whose carrier sorts *after*
the link that names it, generate the inventory from `--archive` and
`--archive-root`, and check the result with the consumer's own
`validate_archive_inventory`, so the archive parsing and the comparison are
exercised end to end rather than through a carrier map handed to the builder.
"""

from __future__ import annotations

import io
import tarfile
import typing as typ

from rust_prover_tools.kani_snapshot.archive_graph import ArchivePolicy
from rust_prover_tools.kani_snapshot.archive_read import read_archive
from rust_prover_tools.kani_snapshot.inventory_archive import (
    validate_archive_inventory,
)
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_FILE,
    KIND_HARDLINK,
)
from rust_prover_tools.kani_snapshot.inventory_parse import parse_inventory
from rust_prover_tools.kani_snapshot.orchestrator.inventory_build import main

if typ.TYPE_CHECKING:
    import pathlib

#: The single root every member of a snapshot archive shares.
_ROOT = "kani-1.0.0"
_CARRIER = f"{_ROOT}/bin/kani"
_LINK = f"{_ROOT}/bin/cargo-kani"
_LAUNCHER = b"launcher"


def _member(
    name: str,
    *,
    kind: bytes = tarfile.REGTYPE,
    mode: int = 0o755,
    data: bytes = b"",
    linkname: str = "",
) -> tuple[tarfile.TarInfo, bytes]:
    """Return one tar header, with the body it declares."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = len(data)
    info.linkname = linkname
    return info, data


def _write_archive(path: pathlib.Path) -> None:
    """Write an archive whose carrier sorts after the link that names it.

    Headers must describe the carrier first, because a hard link names a member
    the archive has already recorded. Path order is the opposite — `cargo-kani`
    sorts before `kani` — so a walk that trusted the tree alone would record
    the carrier the wrong way round. That disagreement is what the archive
    headers are read to settle.
    """
    members = [
        _member(f"{_ROOT}/bin", kind=tarfile.DIRTYPE),
        _member(_CARRIER, data=_LAUNCHER),
        _member(_LINK, kind=tarfile.LNKTYPE, linkname=_CARRIER),
    ]
    with tarfile.open(path, "w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data else None)


def _extract(archive: pathlib.Path, into: pathlib.Path) -> None:
    """Extract the archive as the consumer's installer would."""
    with tarfile.open(archive, "r:gz") as opened:
        opened.extractall(into, filter="data")


def test_the_archive_headers_survive_into_a_validated_inventory(
    tmp_path: pathlib.Path,
) -> None:
    """The inventory records the archive's carrier and passes validation."""
    archive = tmp_path / "bundle.tar.gz"
    _write_archive(archive)
    extracted = tmp_path / "extract"
    extracted.mkdir()
    _extract(archive, extracted)
    inventory_path = tmp_path / "inventory.json"

    code = main([
        "--tree",
        str(extracted / _ROOT),
        "--archive",
        str(archive),
        "--archive-root",
        _ROOT,
        "--output",
        str(inventory_path),
    ])

    assert code == 0, "the inventory of a well-formed archive must be written"
    inventory = parse_inventory(inventory_path.read_bytes())
    entries = {entry.path: entry for entry in inventory.entries}
    assert entries["bin/kani"].kind == KIND_FILE, (
        "the archive's carrier must record the bytes, not the sorted-first name"
    )
    assert entries["bin/cargo-kani"].kind == KIND_HARDLINK
    assert entries["bin/cargo-kani"].link_target == "bin/kani", (
        "the hard link must name the carrier the archive named"
    )
    policy = ArchivePolicy(strip_components=1, required_root=_ROOT)
    validate_archive_inventory(read_archive(archive, policy=policy), inventory)
