"""The release server double the `gh` adapter's observation tests drive.

`cmd_mox` keeps one double per command name, so an observation that needs both
a release document and a tag ref answers them from a single handler rather
than registering competing mocks. That handler, and the tag-object formatting
it relies on, live here so the contract tests can describe the adapter's
arguments while this module describes the server they are observed against.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.kani_snapshot.models import REPOSITORY
from tests import kani_publish_fixtures as fixtures

if typ.TYPE_CHECKING:
    from cmd_mox import Invocation


def _object(kind: str, sha: str) -> str:
    """Return a Git object reference as `--jq .object` prints it."""
    return json.dumps({"type": kind, "sha": sha})


class _ReleaseServer:
    """Answer the release and tag-ref lookups one observation makes.

    `cmd_mox` keeps one double per command name, so a test that needs both a
    release document and a tag ref answers them from a single handler rather
    than registering competing mocks.
    """

    def __init__(
        self,
        *,
        release: str | None,
        objects: dict[str, tuple[str, str]] | None = None,
        listed: str = "[]",
    ) -> None:
        """Record the release document, tag objects and list response."""
        self._release = release
        self._objects = objects or {}
        self._listed = listed
        self.calls: list[list[str]] = []

    def handle(self, invocation: Invocation) -> tuple[str, str, int]:
        """Answer one `gh api` call, recording the argument vector."""
        args = list(invocation.args)
        self.calls.append(args)
        path = args[1]
        if path in self._objects:
            assert args[2:] == ["--jq", ".object"], args
            return (_object(*self._objects[path]), "", 0)
        if self._release is not None and "/releases/tags/" in path:
            assert args[2:] == ["--jq", fixtures.OBSERVE_QUERY], args
            return (self._release, "", 0)
        if path.startswith(f"repos/{REPOSITORY}/releases?"):
            return (self._listed, "", 0)
        return ("", "gh: HTTP 404: Not Found", 1)
