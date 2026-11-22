"""The pre-edit byte check is bounded to the assets it was asked about.

`hash_assets` exists so a draft that needs an edit can be repaired only once
the bytes already present are known to match the manifest. It is therefore a
narrower read than `verify`: it downloads exactly the named assets, reports
their digests and never claims the release authenticated its evidence. These
tests pin that contract by argument vector and by the names it refuses.

The whole-release verification path is checked in `test_kani_gh_release_api`.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release.api import GhReleaseApi
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
    from cmd_mox import CmdMox, Invocation


pytest_plugins = ("cmd_mox.pytest_plugin",)


def test_hash_assets_downloads_only_the_named_assets(
    cmd_mox: CmdMox,
) -> None:
    """The pre-edit byte check downloads exactly what it was asked about.

    Hashes from `hash_assets` are content evidence about the named assets
    only. They are not a provenance verdict, so the adapter must not pay for
    a whole-release download or claim the release authenticated.
    """
    downloaded: list[str] = []

    def handler(invocation: Invocation) -> tuple[str, str, int]:
        """Write the requested asset into the download directory."""
        args = invocation.args
        name = args[args.index("--pattern") + 1]
        downloaded.append(name)
        directory = pathlib.Path(args[args.index("--dir") + 1])
        (directory / name).write_bytes(fixtures.PAYLOADS[name])
        return ("", "", 0)

    cmd_mox.stub("gh").runs(handler)
    observed = fixtures.draft(fixtures.verified_assets())
    named = ("backend.tar.gz", "inventory.json")
    digests = GhReleaseApi().hash_assets(fixtures.TAG, observed, named)
    assert downloaded == list(named), "hashing downloaded more than was named"
    assert dict(digests) == {
        name: fixtures.digest(fixtures.PAYLOADS[name]) for name in named
    }, "hashing did not report the exact bytes the release served"


def test_hash_assets_refuses_names_the_release_does_not_report() -> None:
    """A caller cannot hash bytes the observation never saw."""
    observed = fixtures.draft(fixtures.verified_assets())
    with pytest.raises(ProverToolError, match="does not report"):
        GhReleaseApi().hash_assets(fixtures.TAG, observed, ["absent.tar.gz"])
    with pytest.raises(ProverToolError, match="at least one asset"):
        GhReleaseApi().hash_assets(fixtures.TAG, observed, ())
