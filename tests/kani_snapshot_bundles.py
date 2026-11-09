"""Builders for complete, locally verifiable snapshot release assets.

The payloads are synthetic but structurally complete: real gzip archives, a
real inventory and a real manifest rendered by the production serializer. The
launcher archive also carries a working `cargo-kani setup` stub so the
production setup hook can be exercised end to end without a network or a real
Kani build.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import io
import json
import tarfile
import typing as typ

from rust_prover_tools.kani_snapshot.manifest import manifest_to_json
from rust_prover_tools.kani_snapshot.models import (
    AssetRecord,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    ComponentRecord,
    FileRecord,
    ResourceObservation,
    RuntimeIdentity,
    SnapshotManifest,
    SnapshotPin,
    ToolchainIdentity,
    WorkflowIdentity,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "ATTESTATION_NAME",
    "CHECKSUMS_NAME",
    "KANI_COMPILER",
    "LIBRARY_BYTES",
    "LIBRARY_PATH",
    "MANIFEST_NAME",
    "RUSTC_BYTES",
    "RUSTC_VV",
    "RUSTUP_COMPONENTS",
    "TOOLCHAIN",
    "VERSION",
    "LocalSnapshot",
    "build_local_snapshot",
    "manifest_for",
    "payloads",
    "publish_manifest",
    "sha256_hex",
    "tar_bytes",
    "tree_entry",
    "write_attestation",
    "write_file",
)

VERSION: typ.Final[str] = "0.66.0"
ROOT: typ.Final[str] = f"kani-{VERSION}"
TOOLCHAIN: typ.Final[str] = "nightly-2026-09-23"
RUSTC_VV: typ.Final[str] = "rustc 1.100.0-nightly (abc 2026-09-23)"
KANI_COMPILER: typ.Final[str] = f"kani-compiler {VERSION}"
MANIFEST_NAME: typ.Final[str] = "manifest.json"
CHECKSUMS_NAME: typ.Final[str] = "SHA256SUMS"
ATTESTATION_NAME: typ.Final[str] = "manifest.sigstore.jsonl"

#: role, file name, media type and bytes for one published asset.
Payload = tuple[str, str, str, bytes]

#: Exact bytes setup installs as `bin/rustc`. The manifest records their
#: digest, so the fixture compiler is the bytes a probe would execute.
RUSTC_BYTES: typ.Final[bytes] = f"#!/bin/sh\nprintf '{RUSTC_VV}\\n'\n".encode("ascii")
#: Installed rustup component list, recorded by digest for reuse checks.
RUSTUP_COMPONENTS: typ.Final[bytes] = b"cargo\nrust-std\nrustc\n"
#: One extracted toolchain library, recorded by contained relative path.
LIBRARY_PATH: typ.Final[str] = (
    "lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-fixture.so"
)
LIBRARY_BYTES: typ.Final[bytes] = b"toolchain library fixture\n"
#: Channel manifest digest, recorded for audit but not re-derived by install.
CHANNEL_METADATA_SHA256: typ.Final[str] = hashlib.sha256(
    b"channel manifest fixture\n"
).hexdigest()

_LAUNCHER = b"#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit 7\n"
_COMPILER = f"#!/bin/sh\nprintf '{KANI_COMPILER}\\n'\n".encode("ascii")


def _install_toolchain_file(path: str, delimiter: str, data: bytes) -> str:
    """Return shell lines writing one fixture toolchain file under `$toolchain`."""
    return (
        f"cat > \"$toolchain/{path}\" <<'{delimiter}'\n"
        f"{data.decode('ascii')}"
        f"{delimiter}\n"
    )


_PYTHON_PAYLOAD = (
    'mkdir -p "$kani/pyroot/site/kani"\n'
    "cat > \"$kani/pyroot/site/kani/__init__.py\" <<'PYROOT'\n"
    '"""Fixture Kani Python payload."""\n'
    "PYROOT\n"
)

_SETUP = (
    "#!/bin/sh\n"
    "set -eu\n"
    f'kani="$KANI_HOME/{ROOT}"\n'
    'mkdir -p "$kani"\n'
    'tar -xzf "$3" -C "$kani" --strip-components=1\n'
    'mkdir -p "$kani/pyroot"\n'
    + _PYTHON_PAYLOAD
    + f'toolchain="$RUSTUP_HOME/toolchains/{TOOLCHAIN}"\n'
    'mkdir -p "$toolchain/bin" "$toolchain/lib/rustlib"\n'
    'mkdir -p "$toolchain/lib/rustlib/x86_64-unknown-linux-gnu/lib"\n'
    + _install_toolchain_file("bin/rustc", "RUSTC", RUSTC_BYTES)
    + 'chmod 755 "$toolchain/bin/rustc"\n'
    + _install_toolchain_file("lib/rustlib/components", "COMPONENTS", RUSTUP_COMPONENTS)
    + _install_toolchain_file(LIBRARY_PATH, "LIBRARY", LIBRARY_BYTES)
    + 'ln -sfn "$toolchain" "$kani/toolchain"\n'
).encode("ascii")


@dc.dataclass(frozen=True, slots=True)
class LocalSnapshot:
    """A complete local release plus the pin that selects it.

    Attributes
    ----------
    pin : SnapshotPin
        Reviewed pin whose manifest digest names the written manifest.
    manifest : SnapshotManifest
        Manifest describing the written assets.
    assets_dir : Path
        Directory holding every release file and the attestation stub.
    """

    pin: SnapshotPin
    manifest: SnapshotManifest
    assets_dir: Path


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 of bytes."""
    return hashlib.sha256(data).hexdigest()


def tar_bytes(files: dict[str, tuple[bytes, int]]) -> bytes:
    """Build a gzip tar containing `files` under the bundle root."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo(ROOT)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        directories: set[str] = set()
        for path in files:
            parts = path.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                directory = "/".join((ROOT, *parts[:index]))
                if directory in directories:
                    continue
                directories.add(directory)
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
        for path, (data, mode) in files.items():
            info = tarfile.TarInfo(f"{ROOT}/{path}")
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# One explicit argument per inventory field keeps call sites readable.
# pylint: disable-next=too-many-arguments
def tree_entry(  # ruff: ignore[too-many-arguments] - one inventory field each
    path: str,
    kind: str,
    mode: int,
    *,
    data: bytes | None = None,
    target: str | None = None,
) -> dict[str, object]:
    """Build one inventory entry."""
    entry: dict[str, object] = {"path": path, "kind": kind, "mode": mode}
    if kind == "file":
        if data is None:
            msg = "a file entry needs its bytes"
            raise ValueError(msg)
        entry.update({"size": len(data), "sha256": sha256_hex(data)})
    if target is not None:
        entry["target"] = target
    return entry


def write_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write one fixture file, creating its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def payloads() -> tuple[Payload, ...]:
    """Return the four published payloads as role, name, media type and bytes."""
    driver = b"#!/bin/sh\nexit 0\n"
    compiler = _COMPILER
    backend = tar_bytes({
        "bin/kani-driver": (driver, 0o755),
        "bin/kani-compiler": (compiler, 0o755),
        "share/data.txt": (b"data", 0o644),
    })
    launcher = tar_bytes({
        "bin/kani": (_LAUNCHER, 0o755),
        "bin/cargo-kani": (_SETUP, 0o755),
    })
    notices = tar_bytes({"share/licences.txt": (b"licence", 0o644)})
    inventory = (
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    tree_entry("bin", "directory", 0o755),
                    tree_entry("bin/kani-driver", "file", 0o755, data=driver),
                    tree_entry("bin/kani-compiler", "file", 0o755, data=compiler),
                    tree_entry("share", "directory", 0o755),
                    tree_entry("share/data.txt", "file", 0o644, data=b"data"),
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    return (
        ("backend", "backend.tar.gz", "application/gzip", backend),
        ("launcher", "launcher.tar.gz", "application/gzip", launcher),
        ("notices", "notices.tar.gz", "application/gzip", notices),
        ("inventory", "inventory.json", "application/json", inventory),
    )


def manifest_for(assets: tuple[Payload, ...]) -> SnapshotManifest:
    """Return the manifest describing the fixture payloads."""
    records = tuple(
        AssetRecord(
            role=role,
            name=name,
            media_type=media_type,
            size=len(data),
            sha256=sha256_hex(data),
        )
        for role, name, media_type, data in assets
    )
    return SnapshotManifest(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-bbbbbbbbbbbb-r1",
        upstream_commit="b" * 40,
        builder_commit="c" * 40,
        target="x86_64-unknown-linux-gnu",
        workflow=WorkflowIdentity(
            path=".github/workflows/kani-monthly.yml",
            event="workflow_dispatch",
            ref="refs/heads/main",
            run_id=1,
            run_attempt=1,
        ),
        toolchain=ToolchainIdentity(
            identifier=TOOLCHAIN,
            rustc_vv=RUSTC_VV,
            kani_compiler=KANI_COMPILER,
            channel_metadata_sha256=CHANNEL_METADATA_SHA256,
            components=(
                ComponentRecord(
                    name="rustc",
                    version="1.100.0-nightly",
                    url="https://static.rust-lang.org/dist/2026-09-23/rustc.tar.gz",
                    size=1024,
                    sha256=sha256_hex(b"rustc component fixture\n"),
                ),
            ),
            rustc_sha256=sha256_hex(RUSTC_BYTES),
            rustup_components_sha256=sha256_hex(RUSTUP_COMPONENTS),
            libraries=(
                FileRecord(path=LIBRARY_PATH, sha256=sha256_hex(LIBRARY_BYTES)),
            ),
        ),
        launcher_package_version=VERSION,
        backend=BackendIdentity(version=VERSION, revision="b" * 40),
        runtime=RuntimeIdentity(glibc_baseline="2.34"),
        build=BuildInputs(submodules=(), lock_digests=(), recipe_digest="e" * 64),
        prerequisites=(),
        cache=CacheObservation(outcome="miss"),
        resources=ResourceObservation(
            wall_seconds=0.0,
            disk_high_water_bytes=0,
            asset_bytes=sum(len(data) for _, _, _, data in assets),
        ),
        assets=records,
    )


def publish_manifest(
    assets_dir: Path,
    manifest: SnapshotManifest,
    *,
    base_pin: SnapshotPin,
) -> SnapshotPin:
    """Write the manifest and checksums, returning a pin naming those bytes."""
    raw = manifest_to_json(manifest)
    write_file(assets_dir / MANIFEST_NAME, raw)
    sums = b"".join(
        f"{asset.sha256}  {asset.name}\n".encode("ascii") for asset in manifest.assets
    ) + f"{sha256_hex(raw)}  {MANIFEST_NAME}\n".encode("ascii")
    write_file(assets_dir / CHECKSUMS_NAME, sums)
    return dc.replace(base_pin, manifest_sha256=sha256_hex(raw))


def write_attestation(assets_dir: Path) -> None:
    """Write the placeholder Sigstore bundle named by the manifest."""
    write_file(assets_dir / ATTESTATION_NAME, b'{"bundle":true}\n')


def build_local_snapshot(directory: Path) -> LocalSnapshot:
    """Write a complete local release under `directory` and return its pin."""
    directory.mkdir(parents=True, exist_ok=True)
    assets = payloads()
    for _, name, _, data in assets:
        write_file(directory / name, data)
    manifest = manifest_for(assets)
    base_pin = SnapshotPin(
        schema_version=1,
        repository=manifest.repository,
        tag=manifest.tag,
        upstream_commit=manifest.upstream_commit,
        builder_commit=manifest.builder_commit,
        target=manifest.target,
        manifest_sha256="0" * 64,
    )
    pin = publish_manifest(directory, manifest, base_pin=base_pin)
    write_attestation(directory)
    return LocalSnapshot(pin=pin, manifest=manifest, assets_dir=directory)
