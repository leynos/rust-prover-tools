"""Containment and resource-limit tests for snapshot archive extraction."""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive import (
    ArchiveLimits,
    ArchivePolicy,
    extract_archive,
    normalize_member_path,
    read_archive,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def _info(
    name: str,
    *,
    kind: bytes = tarfile.REGTYPE,
    data: bytes = b"",
    mode: int = 0o644,
    linkname: str = "",
    pax_headers: dict[str, str] | None = None,
) -> tarfile.TarInfo:
    """Return a tar member with explicit effective attributes."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = len(data)
    info.linkname = linkname
    if pax_headers:
        info.pax_headers = pax_headers
    return info


def _write_archive(
    path: Path,
    members: cabc.Sequence[tuple[tarfile.TarInfo, bytes]],
) -> Path:
    """Write a gzip tar archive whose members carry explicit bodies."""
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data else None)
    return path


def _valid_members() -> list[tuple[tarfile.TarInfo, bytes]]:
    """Return an archive with files, directories and contained links."""
    return [
        (_info("bin/", kind=tarfile.DIRTYPE, mode=0o755), b""),
        (_info("bin/tool", data=b"#!/bin/sh\n", mode=0o755), b"#!/bin/sh\n"),
        (_info("share/data.txt", data=b"data\n"), b"data\n"),
        (_info("share/alias.txt", kind=tarfile.SYMTYPE, linkname="data.txt"), b""),
        (_info("bin/tool-hard", kind=tarfile.LNKTYPE, linkname="bin/tool"), b""),
    ]


def test_valid_archive_preserves_files_modes_and_contained_links(
    tmp_path: Path,
) -> None:
    """Ordinary executables and contained hard/symbolic links survive."""
    archive = _write_archive(tmp_path / "asset.tar.gz", _valid_members())
    destination = tmp_path / "out"

    entries = extract_archive(archive, destination)

    assert [entry.path for entry in entries] == [
        "bin",
        "bin/tool",
        "share/data.txt",
        "share/alias.txt",
        "bin/tool-hard",
    ]
    assert (destination / "share/data.txt").read_bytes() == b"data\n"
    assert stat.S_IMODE((destination / "bin/tool").stat().st_mode) == 0o755
    assert str((destination / "share/alias.txt").readlink()) == "data.txt"
    assert (destination / "bin/tool-hard").stat().st_ino == (
        destination / "bin/tool"
    ).stat().st_ino


def test_required_root_is_stripped_before_materialization(tmp_path: Path) -> None:
    """A bundle root is required and removed as setup's tar would remove it."""
    members = [
        (_info("kani-1.0/", kind=tarfile.DIRTYPE, mode=0o755), b""),
        (_info("kani-1.0/bin/kani", data=b"binary", mode=0o755), b"binary"),
        (
            _info(
                "kani-1.0/bin/kani-copy",
                kind=tarfile.LNKTYPE,
                linkname="kani-1.0/bin/kani",
            ),
            b"",
        ),
    ]
    archive = _write_archive(tmp_path / "bundle.tar.gz", members)

    entries = extract_archive(
        archive,
        tmp_path / "out",
        policy=ArchivePolicy(strip_components=1, required_root="kani-1.0"),
    )

    assert [entry.path for entry in entries] == ["bin/kani", "bin/kani-copy"]
    assert (tmp_path / "out/bin/kani").read_bytes() == b"binary"
    assert (tmp_path / "out/bin/kani-copy").is_file()


def test_archive_without_the_required_root_is_rejected(tmp_path: Path) -> None:
    """A bundle must have exactly the expected root component."""
    archive = _write_archive(
        tmp_path / "bundle.tar.gz",
        [(_info("other/bin/kani", data=b"binary"), b"binary")],
    )

    with pytest.raises(ProverToolError, match="root"):
        read_archive(
            archive,
            policy=ArchivePolicy(strip_components=1, required_root="kani-1.0"),
        )


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "a/../../evil",
        "/etc/passwd",
        "C:/evil",
        "a\\b",
        "",
    ],
)
def test_unsafe_member_paths_are_rejected(tmp_path: Path, name: str) -> None:
    """Traversal, absolute, drive, backslash and NUL names fail closed."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info(name, data=b"x"), b"x")],
    )

    with pytest.raises(ProverToolError, match="archive"):
        read_archive(archive)


@pytest.mark.parametrize(
    "name",
    ["a\x00b", "a\\b", "/etc/passwd", "C:/evil", "../evil", ""],
)
def test_normalize_member_path_rejects_unsafe_input(name: str) -> None:
    """Names that tar headers cannot round-trip are still refused directly."""
    with pytest.raises(ProverToolError, match="archive"):
        normalize_member_path(name, max_length=4096)


def test_duplicate_member_paths_are_rejected(tmp_path: Path) -> None:
    """The same effective path may not appear twice."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("./a", data=b"1"), b"1"), (_info("a", data=b"2"), b"2")],
    )

    with pytest.raises(ProverToolError, match="more than once"):
        read_archive(archive)


def test_file_and_directory_collisions_are_rejected(tmp_path: Path) -> None:
    """A file may not also be the ancestor directory of another member."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("a", data=b"file"), b"file"), (_info("a/b", data=b"x"), b"x")],
    )

    with pytest.raises(ProverToolError, match=r"collision|directory"):
        read_archive(archive)


@pytest.mark.parametrize(
    "kind",
    [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE],
    ids=["fifo", "character-device", "block-device"],
)
def test_special_members_are_rejected(tmp_path: Path, kind: bytes) -> None:
    """Devices and FIFOs are never materialized."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("special", kind=kind), b"")],
    )

    with pytest.raises(ProverToolError, match=r"special|unsupported"):
        read_archive(archive)


def test_sparse_members_are_rejected(tmp_path: Path) -> None:
    """GNU sparse metadata is refused even when the type looks regular."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [
            (
                _info(
                    "sparse",
                    data=b"x",
                    pax_headers={"GNU.sparse.major": "1", "GNU.sparse.minor": "0"},
                ),
                b"x",
            )
        ],
    )

    with pytest.raises(ProverToolError, match="sparse"):
        read_archive(archive)


def test_privileged_mode_bits_are_rejected(tmp_path: Path) -> None:
    """Setuid, setgid and sticky bits are not ordinary executable modes."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("tool", data=b"x", mode=0o4755), b"x")],
    )

    with pytest.raises(ProverToolError, match="mode"):
        read_archive(archive)


@pytest.mark.parametrize(
    "linkname",
    ["../outside", "/etc/passwd", "missing", "a/b/../../../outside"],
)
def test_escaping_or_dangling_symbolic_links_are_rejected(
    tmp_path: Path, linkname: str
) -> None:
    """A symbolic link must resolve to a contained graph member."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("a/link", kind=tarfile.SYMTYPE, linkname=linkname), b"")],
    )

    with pytest.raises(ProverToolError, match=r"link|symbolic"):
        read_archive(archive)


def test_symbolic_link_cycles_are_rejected(tmp_path: Path) -> None:
    """A link cycle cannot be materialized deterministically."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [
            (_info("a", kind=tarfile.SYMTYPE, linkname="b"), b""),
            (_info("b", kind=tarfile.SYMTYPE, linkname="a"), b""),
        ],
    )

    with pytest.raises(ProverToolError, match=r"link|cycle"):
        read_archive(archive)


def test_symbolic_link_chain_longer_than_the_hop_limit_is_rejected(
    tmp_path: Path,
) -> None:
    """The link-hop ceiling bounds resolution work and ambiguity."""
    members: list[tuple[tarfile.TarInfo, bytes]] = [(_info("target", data=b"x"), b"x")]
    members.extend(
        (
            _info(
                f"link{index}",
                kind=tarfile.SYMTYPE,
                linkname=("target" if index == 16 else f"link{index + 1}"),
            ),
            b"",
        )
        for index in range(17)
    )
    archive = _write_archive(tmp_path / "asset.tar.gz", members)

    with pytest.raises(ProverToolError, match=r"hop|link"):
        read_archive(archive, limits=ArchiveLimits(max_link_hops=16))


def test_symbolic_link_may_not_be_an_ancestor_of_a_member(
    tmp_path: Path,
) -> None:
    """Writing through a previously planned link is impossible by design."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [
            (_info("target", data=b"x"), b"x"),
            (_info("a", kind=tarfile.SYMTYPE, linkname="target"), b""),
            (_info("a/b", data=b"y"), b"y"),
        ],
    )

    with pytest.raises(ProverToolError, match=r"collision|directory"):
        read_archive(archive)


@pytest.mark.parametrize(
    "linkname",
    ["../outside", "missing", "link"],
)
def test_invalid_hard_links_are_rejected(tmp_path: Path, linkname: str) -> None:
    """Hard links must name a contained regular file."""
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (_info("target", data=b"x"), b"x"),
    ]
    if linkname == "link":
        members.append((_info("link", kind=tarfile.SYMTYPE, linkname="target"), b""))
    members.append((_info("hard", kind=tarfile.LNKTYPE, linkname=linkname), b""))
    archive = _write_archive(tmp_path / "asset.tar.gz", members)

    with pytest.raises(ProverToolError, match="hard link"):
        read_archive(archive)


def test_member_count_limit_is_enforced(tmp_path: Path) -> None:
    """A member-count bomb is stopped while headers are read."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info(f"f{index}", data=b"x"), b"x") for index in range(5)],
    )

    with pytest.raises(ProverToolError, match="members"):
        read_archive(archive, limits=ArchiveLimits(max_members=4))


def test_total_declared_size_limit_is_enforced(tmp_path: Path) -> None:
    """Declared member sizes are summed before materialization."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("a", data=b"12345"), b"12345"), (_info("b", data=b"12345"), b"12345")],
    )

    with pytest.raises(ProverToolError, match="extracted data"):
        read_archive(archive, limits=ArchiveLimits(max_total_bytes=9))


def test_path_length_limit_is_enforced(tmp_path: Path) -> None:
    """A very long member path is refused before materialization."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("a" * 20, data=b"x"), b"x")],
    )

    with pytest.raises(ProverToolError, match="path"):
        read_archive(archive, limits=ArchiveLimits(max_path_length=10))


def test_truncated_member_body_is_reported(tmp_path: Path) -> None:
    """An actual body shorter than its declared size never materializes."""
    archive = tmp_path / "asset.tar.gz"
    # `tarfile.addfile` refuses to write a short body, so build the gzip stream
    # by hand: a valid header promising ten bytes followed by only four.
    info = _info("a", data=b"0123456789")
    archive.write_bytes(gzip.compress(info.tobuf(format=tarfile.PAX_FORMAT) + b"0123"))
    destination = tmp_path / "out"

    with pytest.raises(ProverToolError, match=r"archive|incomplete|size"):
        extract_archive(archive, destination)

    assert not (destination / "a").exists(), "a partial file must not survive"
    assert not destination.exists() or not any(destination.iterdir()), (
        "a failed extraction must not publish member files"
    )


def test_destination_must_be_empty(tmp_path: Path) -> None:
    """Extraction never merges with pre-existing content."""
    archive = _write_archive(
        tmp_path / "asset.tar.gz",
        [(_info("a", data=b"x"), b"x")],
    )
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "existing").write_text("keep", encoding="utf-8")

    with pytest.raises(ProverToolError, match="empty"):
        extract_archive(archive, destination)

    assert (destination / "existing").read_text(encoding="utf-8") == "keep"


def test_member_order_permutations_preserve_the_result(tmp_path: Path) -> None:
    """Files are created before links regardless of header order."""
    members = _valid_members()
    forward = _write_archive(tmp_path / "forward.tar.gz", members)
    reverse = _write_archive(tmp_path / "reverse.tar.gz", list(reversed(members)))

    extract_archive(forward, tmp_path / "one")
    extract_archive(reverse, tmp_path / "two")

    assert (tmp_path / "one/share/alias.txt").readlink() == (
        tmp_path / "two/share/alias.txt"
    ).readlink()
    assert (tmp_path / "one/bin/tool-hard").read_bytes() == (
        tmp_path / "two/bin/tool-hard"
    ).read_bytes()


_SAFE_COMPONENT = st.from_regex(r"[a-z][a-z0-9_]{0,6}", fullmatch=True)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(components=st.lists(_SAFE_COMPONENT, min_size=1, max_size=4, unique=True))
def test_generated_contained_paths_normalize_without_escape(
    components: list[str],
) -> None:
    """Generated safe paths stay inside the extraction root."""
    name = "/".join(components)
    assert normalize_member_path(name, max_length=4096) == name


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    prefix=st.lists(_SAFE_COMPONENT, min_size=1, max_size=3),
    suffix=st.lists(st.sampled_from([".."]), min_size=1, max_size=3),
)
def test_generated_parent_traversal_is_rejected(
    prefix: list[str],
    suffix: list[str],
) -> None:
    """Any effective parent component is refused."""
    name = "/".join([*prefix, *suffix])
    with pytest.raises(ProverToolError, match="archive"):
        normalize_member_path(name, max_length=4096)


def test_symbolic_link_creates_its_implicit_parent_directory(
    tmp_path: Path,
) -> None:
    """A valid contained link is extractable when its parent is implicit."""
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (_info("data.txt", data=b"payload\n"), b"payload\n"),
        (
            _info("share/alias.txt", kind=tarfile.SYMTYPE, linkname="../data.txt"),
            b"",
        ),
    ]
    archive = _write_archive(tmp_path / "asset.tar.gz", members)
    destination = tmp_path / "out"

    extract_archive(archive, destination)

    assert (destination / "share").is_dir()
    assert str((destination / "share/alias.txt").readlink()) == "../data.txt"
    assert (destination / "share/alias.txt").read_bytes() == b"payload\n"


def test_hard_link_creates_its_implicit_parent_directory(
    tmp_path: Path,
) -> None:
    """A hard link is extractable when its parent is not an archive member."""
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (_info("bin/tool", data=b"#!/bin/sh\n", mode=0o755), b"#!/bin/sh\n"),
        (
            _info("link/tool-hard", kind=tarfile.LNKTYPE, linkname="bin/tool"),
            b"",
        ),
    ]
    archive = _write_archive(tmp_path / "asset.tar.gz", members)
    destination = tmp_path / "out"

    extract_archive(archive, destination)

    assert (destination / "link").is_dir()
    assert (destination / "link/tool-hard").stat().st_ino == (
        destination / "bin/tool"
    ).stat().st_ino
