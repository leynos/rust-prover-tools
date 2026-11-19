"""Identity allocation needs the releases that already exist.

An allocator that cannot see the newest tag will happily reuse it, so the
resolve job's release read is a correctness boundary rather than
bookkeeping. These tests pin the projection, the pagination, the refusal to
truncate, and the fact that the collector's document is exactly what
`identity` accepts.
"""

from __future__ import annotations

import json
import re
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_transport import gh
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.producer.collect_releases import (
    PAGE_SIZE,
    RELEASE_PAGE_LIMIT,
    collect_releases,
    main,
)
from rust_prover_tools.kani_snapshot.producer.entrypoint import main as entrypoint
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import resolution_to_json
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    parse_publications,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.kani_snapshot.release_model import (
    ReleaseTransientError,
    release_body,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)

_PAGE_ARGUMENT: typ.Final[re.Pattern[str]] = re.compile(r"[?&]page=(?P<page>\d+)")

#: A failure message an unreachable server would produce.
_SERVER_UNAVAILABLE: typ.Final[str] = "HTTP 503: Service Unavailable"


def _serve(
    pages: cabc.Mapping[int, cabc.Sequence[dict[str, object]]],
) -> cabc.Callable[[Invocation], tuple[str, str, int]]:
    """Return a `gh` handler serving the paged release list."""

    def handle(invocation: Invocation) -> tuple[str, str, int]:
        """Answer one page, refusing any other call shape."""
        arguments = list(invocation.args)
        if arguments[:1] != ["api"] or "--jq" not in arguments:
            msg = f"unexpected gh call: {arguments}"
            raise AssertionError(msg)
        match = _PAGE_ARGUMENT.search(arguments[1])
        if match is None:
            msg = f"release URL has no page: {arguments[1]}"
            raise AssertionError(msg)
        page = int(match.group("page"))
        if page not in pages:
            msg = f"unexpected release page {page}"
            raise AssertionError(msg)
        return (json.dumps(list(pages[page])), "", 0)

    return handle


def _release(tag: str, **extra: object) -> dict[str, object]:
    """Return one release API record with the fields the query projects."""
    return {
        "tag": tag,
        "builder_commit": fixtures.BUILDER_COMMIT,
        "draft": False,
        "body": None,
        **extra,
    }


def test_the_collector_projects_only_the_identity_fields(
    cmd_mox: CmdMox,
) -> None:
    """A field identity allocation does not read is not carried forward."""
    cmd_mox.stub("gh").runs(
        _serve({1: [_release(fixtures.TAG, name="notes to ignore")]})
    )

    document = json.loads(collect_releases(run=gh))

    assert document == [
        {
            "tag": fixtures.TAG,
            "builder_commit": fixtures.BUILDER_COMMIT,
            "draft": False,
            "body": None,
        }
    ]


def test_a_full_page_is_followed_by_the_next(cmd_mox: CmdMox) -> None:
    """A full page does not prove the list ended, so the next page is read."""
    first = [
        _release(f"kani-snapshot-2026-01-{'a' * 12}-r{index}")
        for index in range(PAGE_SIZE)
    ]
    second = [_release(fixtures.TAG)]
    cmd_mox.stub("gh").runs(_serve({1: first, 2: second}))

    document = json.loads(collect_releases(run=gh))

    assert [record["tag"] for record in document][-1] == fixtures.TAG
    assert len(document) == PAGE_SIZE + 1


def test_an_exhausted_release_list_is_refused(cmd_mox: CmdMox) -> None:
    """A truncated view could hide the newest revision."""
    page = [
        _release(f"kani-snapshot-2026-01-{'a' * 12}-r{index}")
        for index in range(PAGE_SIZE)
    ]
    pages = dict.fromkeys(range(1, RELEASE_PAGE_LIMIT + 1), page)
    cmd_mox.stub("gh").runs(_serve(pages))

    with pytest.raises(ProverToolError, match="bound"):
        collect_releases(run=gh)


def test_a_release_without_a_tag_is_refused(cmd_mox: CmdMox) -> None:
    """A tagless release cannot inform revision allocation."""
    cmd_mox.stub("gh").runs(_serve({1: [_release("")]}))

    with pytest.raises(ProverToolError, match="tag"):
        collect_releases(run=gh)


def test_a_failed_call_is_refused(cmd_mox: CmdMox) -> None:
    """An unread list is not evidence that no release exists."""

    def fail(invocation: Invocation) -> tuple[str, str, int]:
        """Answer with a server failure."""
        return ("", "gh: HTTP 500", 1)

    cmd_mox.stub("gh").runs(fail)

    with pytest.raises(ProverToolError, match="gh failed"):
        collect_releases(run=gh)


def test_the_collector_asks_for_each_page_by_its_exact_arguments() -> None:
    """The request is asserted without starting a process.

    Injecting the runner makes the command itself the unit under test: a page
    number or projection that drifted would otherwise only be caught by a live
    or mocked process boundary.
    """
    calls: list[tuple[str, ...]] = []

    def record(
        argv: cabc.Sequence[str], *, timeout_seconds: float | None = None
    ) -> str:
        """Record the request and answer with one short page."""
        calls.append(tuple(argv))
        return json.dumps([_release(fixtures.TAG)])

    collect_releases(run=record)

    (call,) = calls
    assert call[:4] == (
        "gh",
        "api",
        f"repos/{REPOSITORY}/releases?per_page={PAGE_SIZE}&page=1",
        "--jq",
    )
    assert ".tag_name" in call[4]
    assert ".body" in call[4]


def test_a_transient_transport_failure_stays_transient() -> None:
    """Bounding the message must not hide that the read may still succeed."""

    def unavailable(
        argv: cabc.Sequence[str], *, timeout_seconds: float | None = None
    ) -> str:
        """Fail the way an unreachable server does."""
        raise ReleaseTransientError(_SERVER_UNAVAILABLE)

    with pytest.raises(ReleaseTransientError, match="release page 1"):
        collect_releases(run=unavailable)


def test_the_command_writes_the_release_document(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The resolve job runs a command, not an imported function."""
    cmd_mox.stub("gh").runs(_serve({1: [_release(fixtures.TAG)]}))
    output = tmp_path / "releases.json"

    assert main(["--output", str(output)]) == 0

    assert parse_publications(output.read_bytes())[0].tag == fixtures.TAG


def test_collected_releases_drive_identity_allocation(
    tmp_path: pathlib.Path, cmd_mox: CmdMox
) -> None:
    """The collector's document is exactly what `identity` accepts."""
    workflow = fixtures.workflow()
    fingerprint = input_fingerprint(
        upstream_commit=fixtures.UPSTREAM_COMMIT,
        builder_commit=fixtures.BUILDER_COMMIT,
        recipe=pinned_recipe(),
    )
    published = _release(
        fixtures.TAG,
        body=release_body(
            fixtures.TAG,
            fingerprint,
            run_id=workflow.run_id,
            run_attempt=workflow.run_attempt,
        ),
    )
    cmd_mox.stub("gh").runs(_serve({1: [published]}))
    releases = tmp_path / "releases.json"
    assert main(["--output", str(releases)]) == 0
    resolution = tmp_path / "resolution.json"
    resolution.write_bytes(resolution_to_json(fixtures.resolution()))

    assert (
        entrypoint([
            "identity",
            "--resolution",
            str(resolution),
            "--builder-commit",
            fixtures.BUILDER_COMMIT,
            "--month",
            "2026-09",
            "--releases",
            str(releases),
            "--output",
            str(tmp_path / "identity.json"),
            "--run-id",
            str(workflow.run_id),
            "--run-attempt",
            str(workflow.run_attempt),
            "--event",
            workflow.event,
        ])
        == 0
    )
