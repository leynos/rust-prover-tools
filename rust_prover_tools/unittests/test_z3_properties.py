"""Property tests for the digest-bound Z3 installation marker.

The unit tests enumerate the rejection classes. These properties state the
invariant over arbitrary marker text and installed bytes: only a canonical
marker for the pinned version and a lowercase SHA-256 digest is usable, and a
reuse decision matches the bytes on disk.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import rust_prover_tools.z3 as z3_module
from rust_prover_tools.binary_assets import Z3_VERSION

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

_HEX_CHARACTERS = "0123456789abcdef"
_LOWER_HEX = frozenset(_HEX_CHARACTERS)
_HEX_DIGESTS = st.text(alphabet=_HEX_CHARACTERS, min_size=64, max_size=64)

# Hypothesis cannot reset function-scoped fixtures between examples. Each
# example writes its own files and installs the same replacement, so reuse is
# safe.
_REUSABLE_FIXTURES = [HealthCheck.function_scoped_fixture]


def _marker_text(digest: object) -> str:
    """Return canonical marker text wrapping a digest-shaped value."""
    return json.dumps({"version": Z3_VERSION, "sha256": digest}, sort_keys=True) + "\n"


@given(digest=_HEX_DIGESTS)
@settings(max_examples=50, deadline=None)
def test_recorded_digest_accepts_canonical_markers(digest: str) -> None:
    """Every lowercase hex digest in a canonical marker is read back."""
    assert z3_module._recorded_digest(_marker_text(digest)) == digest


@given(
    digest=st.text(min_size=64, max_size=64).filter(
        lambda value: any(char not in _LOWER_HEX for char in value),
    ),
)
@settings(max_examples=50, deadline=None)
def test_recorded_digest_rejects_non_lowercase_hex(digest: str) -> None:
    """A 64-character digest containing any other character is rejected."""
    assert z3_module._recorded_digest(_marker_text(digest)) is None


@given(
    digest=st.text(alphabet=_HEX_CHARACTERS).filter(
        lambda value: len(value) != 64,
    ),
)
@settings(max_examples=50, deadline=None)
def test_recorded_digest_rejects_other_hex_lengths(digest: str) -> None:
    """A lowercase hex digest of any length other than 64 is rejected."""
    assert z3_module._recorded_digest(_marker_text(digest)) is None


@given(text=st.text())
@settings(max_examples=50, deadline=None)
def test_recorded_digest_is_total(text: str) -> None:
    """Arbitrary text yields either no digest or a lowercase SHA-256 digest."""
    digest = z3_module._recorded_digest(text)
    if digest is not None:
        assert len(digest) == 64
        assert set(digest) <= _LOWER_HEX


@given(payload=st.binary(min_size=1, max_size=256))
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=_REUSABLE_FIXTURES,
)
def test_install_reuses_a_binary_whose_bytes_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linux_x86_64_host: None,
    payload: bytes,
) -> None:
    """An executable whose bytes match the marker digest is reused."""
    del linux_x86_64_host
    install_dir = tmp_path / "install"
    install_dir.mkdir(exist_ok=True)
    binary = install_dir / "z3"
    binary.write_bytes(payload)
    binary.chmod(0o755)
    (install_dir / z3_module.MARKER_NAME).write_text(
        _marker_text(hashlib.sha256(payload).hexdigest()),
        encoding="utf-8",
    )

    def fail_download(asset: object, destination: Path) -> Path:
        """Fail the test if a matching installation is downloaded again."""
        del asset, destination
        msg = "a matching installation must not be downloaded again"
        raise AssertionError(msg)

    monkeypatch.setattr(z3_module, "download_pinned", fail_download)

    lines = z3_module.install_z3(
        z3_module.Z3InstallOptions(
            repo_root=tmp_path,
            install_dir=install_dir,
            cache_dir=tmp_path / "cache",
        ),
    )

    assert lines[0] == f"Z3 {Z3_VERSION} already installed at {binary}"
