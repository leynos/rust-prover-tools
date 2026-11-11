"""Containment and resource-limit tests for snapshot archive extraction."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import gzip
import io
import os
import posixpath
import random
import stat
import tarfile
import tempfile
import typing as typ
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot import archive_extract
from rust_prover_tools.kani_snapshot.archive import (
    ArchiveLimits,
    ArchivePolicy,
    extract_archive,
    normalize_member_path,
    read_archive,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.progress import Progress


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


def test_read_archive_reports_progress_while_skipping_a_large_member(
    tmp_path: Path,
) -> None:
    """Scanning must checkpoint inside a large member, not only per header.

    `tarfile` reaches the next header by reading through the compressed body,
    so a one-member archive whose body is skipped between two headers used to
    spend the whole skip without reporting progress. The body is incompressible
    on purpose: a compressible one fits in a single decompressor fill, so the
    skip would never need a second raw read and would not witness the fix.
    """
    # Not a security boundary: deterministic bytes that merely compress badly.
    body = random.Random(20261109).randbytes(4 * 1024 * 1024)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    archive = _write_archive(
        tmp_path / "large.tar.gz",
        [(_info("blob.bin", data=body), body)],
    )
    calls: list[None] = []

    def checkpoint() -> None:
        """Expire the budget well after the first member's header."""
        calls.append(None)
        if len(calls) > 16:
            msg = "deadline expired during archive scan"
            raise ProverToolError(msg)

    with pytest.raises(ProverToolError, match=r"archive scan"):
        read_archive(archive, checkpoint=checkpoint)

    assert len(calls) > 16, "the skip never reported progress"


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


def test_missing_extraction_parents_are_created_private(tmp_path: Path) -> None:
    """Parents created under a permissive umask are still owner-only.

    `Path.mkdir(parents=True)` would give each missing level the default mode
    masked by the umask, so a umask of `0002` would produce `0o775` parents
    that the ancestor check rejects. Extraction must create them 0700.
    """
    archive = _write_archive(tmp_path / "asset.tar.gz", _valid_members())
    destination = tmp_path / "cache" / "downloads" / "launcher"

    previous = os.umask(0o002)
    try:
        extract_archive(archive, destination)
    finally:
        os.umask(previous)

    for parent in (tmp_path / "cache", tmp_path / "cache" / "downloads"):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700, (
            f"extraction created {parent.name} with the umask default"
        )
    assert (destination / "share/data.txt").read_bytes() == b"data\n"


def test_a_materialization_pass_must_match_the_validated_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file replaced after validation cannot introduce an unseen member.

    Both materialization passes re-open the archive, so a swap between
    validation and the second pass would otherwise smuggle in a link the graph
    rules never approved. Here the replacement only changes a symbolic link's
    target, which the second pass must refuse.
    """
    archive = _write_archive(tmp_path / "asset.tar.gz", _valid_members())
    changed_members = _valid_members()
    changed_members[3] = (
        _info("share/alias.txt", kind=tarfile.SYMTYPE, linkname="../../etc"),
        b"",
    )
    changed = _write_archive(tmp_path / "changed.tar.gz", changed_members)

    real_open = archive_extract._open_archive
    opened: list[Path] = []

    @contextlib.contextmanager
    def alternating_open(
        archive_path: Path,
        checkpoint: Progress,
    ) -> cabc.Generator[tarfile.TarFile]:
        """Serve the validated archive first, then the replacement."""
        effective = archive_path if not opened else changed
        opened.append(effective)
        with real_open(effective, checkpoint) as archive_handle:
            yield archive_handle

    monkeypatch.setattr(archive_extract, "_open_archive", alternating_open)
    destination = tmp_path / "out"

    with pytest.raises(ProverToolError, match="changed after validation"):
        extract_archive(archive, destination)

    assert not destination.exists(), "a refused extraction published a tree"


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


# Generated member graphs complement the named fixtures above. They vary the
# shape of a valid archive (depth, member order, link mix) and compare the
# extracted tree with an oracle built from the generator's own knowledge, so a
# containment or link-resolution regression cannot hide behind hand-picked
# examples. They still complement rather than prove arbitrary filesystem
# safety.


@dc.dataclass(frozen=True, slots=True)
class _Graph:
    """A generated archive and the tree it must produce."""

    members: list[tuple[tarfile.TarInfo, bytes]]
    directories: tuple[str, ...]
    files: dict[str, tuple[bytes, int]]
    symlinks: dict[str, str]
    hardlinks: dict[str, str]


@contextlib.contextmanager
def _scratch_directory() -> cabc.Generator[Path]:
    """Yield a fresh directory for one generated example.

    Hypothesis does not reset a function-scoped fixture between examples, so
    each example gets its own disposable root instead of sharing `tmp_path`.
    """
    with tempfile.TemporaryDirectory(prefix="kani-archive-") as raw:
        yield Path(raw)


def _ancestor_paths(path: str) -> tuple[str, ...]:
    """Return every proper ancestor of an archive path."""
    parts = path.split("/")
    return tuple("/".join(parts[:index]) for index in range(1, len(parts)))


def _draw_paths(draw: st.DrawFn) -> list[str]:
    """Draw unique contained paths that all share one depth.

    Equal depth rules out a path that is also another member's ancestor, so
    the resulting graph never needs a file/directory collision decision.
    """
    depth = draw(st.integers(min_value=1, max_value=3))
    # Join inside the strategy: `unique=True` compares drawn values in a set,
    # and component lists are unhashable while their joined paths are not.
    path_strategy = st.lists(_SAFE_COMPONENT, min_size=depth, max_size=depth).map(
        "/".join
    )
    # Ten depth-three paths plus their ancestors reach thirty members, the
    # graph size the verification plan asks these tests to cover.
    return draw(st.lists(path_strategy, min_size=2, max_size=10, unique=True))


def _draw_files(
    draw: st.DrawFn,
    paths: list[str],
) -> tuple[list[str], dict[str, bytes], dict[str, int]]:
    """Draw the graph's regular files with their bodies and modes."""
    files = draw(
        st.lists(
            st.sampled_from(paths),
            min_size=1,
            max_size=min(len(paths), 6),
            unique=True,
        )
    )
    bodies = draw(
        st.lists(st.binary(max_size=32), min_size=len(files), max_size=len(files))
    )
    modes = draw(
        st.lists(
            st.sampled_from([0o644, 0o755]),
            min_size=len(files),
            max_size=len(files),
        )
    )
    return (
        files,
        dict(zip(files, bodies, strict=True)),
        dict(zip(files, modes, strict=True)),
    )


def _draw_links(
    draw: st.DrawFn,
    files: list[str],
    links: list[str],
) -> tuple[
    list[tuple[tarfile.TarInfo, bytes]],
    dict[str, str],
    dict[str, str],
]:
    """Draw contained symbolic and hard links to the graph's files."""
    kinds = draw(
        st.lists(
            st.sampled_from([tarfile.SYMTYPE, tarfile.LNKTYPE]),
            min_size=len(links),
            max_size=len(links),
        )
    )
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    symlinks: dict[str, str] = {}
    hardlinks: dict[str, str] = {}
    for path, kind in zip(links, kinds, strict=True):
        target = draw(st.sampled_from(files))
        if kind == tarfile.LNKTYPE:
            hardlinks[path] = target
            members.append((_info(path, kind=kind, linkname=target), b""))
        else:
            linkname = posixpath.relpath(target, posixpath.dirname(path) or ".")
            symlinks[path] = linkname
            members.append((_info(path, kind=kind, linkname=linkname), b""))
    return members, symlinks, hardlinks


@st.composite
def _contained_graphs(draw: st.DrawFn) -> _Graph:
    """Draw a member graph whose every write and link stays contained.

    Links point at files in the same graph, never outside it, and the link
    path set stays disjoint from the file set.
    """
    paths = _draw_paths(draw)
    files, bodies, modes = _draw_files(draw, paths)
    directories = tuple(
        sorted({ancestor for path in paths for ancestor in _ancestor_paths(path)})
    )
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (_info(path + "/", kind=tarfile.DIRTYPE, mode=0o755), b"")
        for path in directories
    ]
    members.extend(
        (_info(path, data=bodies[path], mode=modes[path]), bodies[path])
        for path in files
    )
    link_members, symlinks, hardlinks = _draw_links(
        draw, files, [path for path in paths if path not in files]
    )
    members.extend(link_members)
    return _Graph(
        members=members,
        directories=directories,
        files={path: (bodies[path], modes[path]) for path in files},
        symlinks=symlinks,
        hardlinks=hardlinks,
    )


def _assert_matches_graph(destination: Path, graph: _Graph) -> None:
    """Assert the extracted tree equals the generator's expectation."""
    root = destination.resolve()
    for path in graph.directories:
        assert (destination / path).is_dir(), f"missing directory {path}"
    for path, (body, mode) in graph.files.items():
        extracted = destination / path
        assert extracted.is_file(), f"missing file {path}"
        assert extracted.read_bytes() == body, f"wrong body for {path}"
        assert stat.S_IMODE(extracted.stat().st_mode) == mode, f"wrong mode for {path}"
    for path, target in graph.symlinks.items():
        link = destination / path
        assert link.is_symlink(), f"{path} is not a symbolic link"
        assert str(link.readlink()) == target, f"{path} has the wrong target"
        resolved = link.resolve()
        assert resolved == root or root in resolved.parents, f"{path} escapes the root"
    for path, target in graph.hardlinks.items():
        assert (destination / path).stat().st_ino == (
            destination / target
        ).stat().st_ino, f"{path} is not a hard link to {target}"


def _outside_sentinel(tmp_path: Path) -> tuple[Path, Path]:
    """Create the outside tree that extraction must never touch."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    return outside, sentinel


def _assert_refused_without_outside_writes(
    tmp_path: Path, archive: Path, match: str
) -> None:
    """Assert a hostile archive is refused and leaves no trace outside."""
    outside, sentinel = _outside_sentinel(tmp_path)
    destination = tmp_path / "out"

    with pytest.raises(ProverToolError, match=match):
        extract_archive(archive, destination)

    assert sentinel.read_text(encoding="utf-8") == "keep", "outside file changed"
    assert sorted(entry.name for entry in outside.iterdir()) == ["sentinel"], (
        "extraction wrote outside the destination"
    )
    assert not destination.exists() or not any(destination.iterdir()), (
        "a refused archive published members"
    )


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(graph=_contained_graphs())
def test_generated_contained_graphs_extract_inside_the_destination(
    graph: _Graph,
) -> None:
    """Generated valid graphs materialize exactly as their oracle predicts."""
    with _scratch_directory() as root:
        archive = _write_archive(root / "asset.tar.gz", graph.members)
        outside, sentinel = _outside_sentinel(root)
        destination = root / "out"

        extract_archive(archive, destination)

        _assert_matches_graph(destination, graph)
        assert sentinel.read_text(encoding="utf-8") == "keep", "outside file changed"
        assert sorted(entry.name for entry in outside.iterdir()) == ["sentinel"], (
            "extraction wrote outside the destination"
        )
        assert sorted(entry.name for entry in root.iterdir()) == [
            "asset.tar.gz",
            "out",
            "outside",
        ], "extraction created an unexpected top-level entry"


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(graph=_contained_graphs())
def test_generated_member_order_permutations_extract_identically(
    graph: _Graph,
) -> None:
    """Header order does not change the materialized tree or its links."""
    with _scratch_directory() as root:
        forward = _write_archive(root / "forward.tar.gz", graph.members)
        reverse = _write_archive(root / "reverse.tar.gz", list(reversed(graph.members)))

        extract_archive(forward, root / "forward")
        extract_archive(reverse, root / "reverse")

        _assert_matches_graph(root / "forward", graph)
        _assert_matches_graph(root / "reverse", graph)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(length=st.integers(min_value=1, max_value=16))
def test_generated_link_chains_within_the_hop_limit_are_accepted(
    length: int,
) -> None:
    """A chain of at most sixteen links resolves to its contained file."""
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (_info("target", data=b"payload\n"), b"payload\n")
    ]
    for index in range(length):
        next_link = "target" if index == length - 1 else f"link{index + 1}"
        members.append((
            _info(f"link{index}", kind=tarfile.SYMTYPE, linkname=next_link),
            b"",
        ))
    with _scratch_directory() as root:
        archive = _write_archive(root / "asset.tar.gz", members)
        destination = root / "out"

        extract_archive(archive, destination)

        assert (destination / "link0").read_bytes() == b"payload\n"


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(parts=st.lists(_SAFE_COMPONENT, min_size=1, max_size=3))
def test_generated_parent_traversal_never_writes_outside(
    parts: list[str],
) -> None:
    """A traversal path is refused before any outside write happens."""
    escape = "/".join([*parts, *([".."] * (len(parts) + 1)), "outside"])
    with _scratch_directory() as root:
        archive = _write_archive(
            root / "asset.tar.gz", [(_info(escape, data=b"x"), b"x")]
        )

        _assert_refused_without_outside_writes(root, archive, "archive")


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    directory=st.lists(_SAFE_COMPONENT, min_size=1, max_size=3),
    name=_SAFE_COMPONENT,
)
def test_generated_escaping_symbolic_links_never_write_outside(
    directory: list[str],
    name: str,
) -> None:
    """A symbolic link climbing past the root is refused."""
    linkname = "../" * len(directory) + "outside"
    member = _info(
        "/".join([*directory, name]), kind=tarfile.SYMTYPE, linkname=linkname
    )
    with _scratch_directory() as root:
        archive = _write_archive(root / "asset.tar.gz", [(member, b"")])

        _assert_refused_without_outside_writes(root, archive, r"link|symbolic")


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    directory=st.lists(_SAFE_COMPONENT, min_size=1, max_size=2),
    length=st.integers(min_value=2, max_value=6),
)
def test_generated_symbolic_link_cycles_are_refused(
    directory: list[str],
    length: int,
) -> None:
    """Every generated cycle is refused, whatever its length."""
    names = [f"link{index}" for index in range(length)]
    members: list[tuple[tarfile.TarInfo, bytes]] = [
        (
            _info(
                "/".join([*directory, name]),
                kind=tarfile.SYMTYPE,
                linkname=names[(index + 1) % length],
            ),
            b"",
        )
        for index, name in enumerate(names)
    ]
    with _scratch_directory() as root:
        archive = _write_archive(root / "asset.tar.gz", members)

        _assert_refused_without_outside_writes(root, archive, r"link|cycle")


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    parts=st.lists(_SAFE_COMPONENT, min_size=1, max_size=3),
    first=st.binary(max_size=8),
    second=st.binary(max_size=8),
)
def test_generated_duplicate_member_paths_are_refused(
    parts: list[str],
    first: bytes,
    second: bytes,
) -> None:
    """The same effective path twice is refused before materialization."""
    name = "/".join(parts)
    with _scratch_directory() as root:
        archive = _write_archive(
            root / "asset.tar.gz",
            [(_info(name, data=first), first), (_info(name, data=second), second)],
        )

        _assert_refused_without_outside_writes(root, archive, "more than once")


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    parts=st.lists(_SAFE_COMPONENT, min_size=1, max_size=3),
    kind=st.sampled_from([tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]),
)
def test_generated_special_members_are_refused(
    parts: list[str],
    kind: bytes,
) -> None:
    """Devices and FIFOs never reach the filesystem."""
    with _scratch_directory() as root:
        archive = _write_archive(
            root / "asset.tar.gz",
            [(_info("/".join(parts), kind=kind), b"")],
        )

        _assert_refused_without_outside_writes(root, archive, r"special|unsupported")


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    body=st.binary(min_size=2, max_size=32),
    slack=st.integers(min_value=1, max_value=16),
    actual=st.integers(min_value=0, max_value=1),
)
def test_generated_size_bombs_are_refused(
    body: bytes,
    slack: int,
    actual: int,
) -> None:
    """Declared sizes beyond the budget are refused before extraction.

    `actual` selects whether the archive also declares more bytes than it
    carries; either way the limit must be enforced from the headers alone.
    """
    declared = body if actual else body + b"x" * slack
    with _scratch_directory() as root:
        archive = _write_archive(
            root / "asset.tar.gz", [(_info("a", data=declared), declared)]
        )
        outside, sentinel = _outside_sentinel(root)
        destination = root / "out"

        with pytest.raises(ProverToolError, match=r"extracted data|archive"):
            extract_archive(
                archive,
                destination,
                limits=ArchiveLimits(max_total_bytes=len(declared) - 1),
            )

        assert sentinel.read_text(encoding="utf-8") == "keep", "outside file changed"
        assert sorted(entry.name for entry in outside.iterdir()) == ["sentinel"], (
            "extraction wrote outside the destination"
        )
        assert not destination.exists() or not any(destination.iterdir()), (
            "a refused archive published members"
        )


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(body=st.binary(min_size=2, max_size=32))
def test_generated_truncated_member_bodies_are_refused(
    body: bytes,
) -> None:
    """A stream that ends early never leaves a partial file behind."""
    info = _info("partial", data=body)
    truncation = len(body) // 2
    with _scratch_directory() as root:
        archive = root / "asset.tar.gz"
        archive.write_bytes(
            gzip.compress(info.tobuf(format=tarfile.PAX_FORMAT) + body[:truncation])
        )
        destination = root / "out"

        with pytest.raises(ProverToolError, match=r"archive|incomplete|size"):
            extract_archive(archive, destination)

        assert not (destination / "partial").exists(), "a partial file survived"
        assert not destination.exists() or not any(destination.iterdir()), (
            "a failed extraction must not publish member files"
        )
