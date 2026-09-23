"""Find what a pull request can reach, and refuse CodeScene anywhere in it.

The pull-request surface is a closure rather than a trigger list. A
workflow declaring only `workflow_call` still runs on a pull request when
a pull-request job calls it, and `secrets: inherit` hands it every secret,
so a reading that enumerated triggers alone could not see it at all.
"""

from __future__ import annotations

import re
import typing as typ

from .loading import Document, WorkflowReadingError
from .reading import PULL_REQUEST_TRIGGERS, jobs, texts, triggers

#: Where a same-repository reusable workflow lives.
WORKFLOW_DIRECTORY: typ.Final[str] = ".github/workflows/"

#: Case-folded patterns no pull-request-reachable document may contain:
#: the CodeScene host (a DNS name, so case-insensitive), the credential,
#: the client, the shared uploader and the retired checksum plumbing.
#: They are prohibitions, so a false match fails loudly rather than
#: hiding anything.
CODESCENE_MARKERS: typ.Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"codescene\.io"),
    re.compile(r"cs_access_token"),
    re.compile(r"(?<![\w-])cs-coverage(?![\w-])"),
    re.compile(r"upload-codescene-coverage"),
    re.compile(r"codescene_cli_sha256"),
    re.compile(r"installer-checksum"),
)


def _is_chained_on_a_run(document: Document) -> bool:
    """Return whether a workflow runs after another workflow's run.

    A `workflow_run` workflow runs with secrets after whatever it names,
    which may be a pull-request workflow, so it is treated as reachable.
    """
    return "workflow_run" in triggers(document)


def is_pull_request_seed(document: Document) -> bool:
    """Return whether a workflow is started directly by a pull request."""
    return bool(triggers(document) & PULL_REQUEST_TRIGGERS) or (
        _is_chained_on_a_run(document)
    )


def local_callee(reference: str, repository: str) -> str | None:
    """Return the workflow file a job-level `uses:` names in this tree.

    Matched by shape: strip a leading `./` or `$/` and ask whether the
    rest is a file under the workflow directory. A reference to another
    repository is not followed, since its content is not here.

    Raises
    ------
    WorkflowReadingError
        If a local spelling carries an `@ref`, or the reference names this
        repository qualified by a ref: either runs a version of the file
        this checkout does not hold, so following it would prove nothing.

    Examples
    --------
    >>> local_callee("$/.github/workflows/x.yml", "leynos/example")
    'x.yml'
    >>> local_callee("leynos/other/.github/workflows/x.yml@main", "leynos/example")

    """
    qualified_self = f"{repository}/{WORKFLOW_DIRECTORY}".casefold()
    if reference.casefold().startswith(qualified_self):
        message = f"{reference!r} calls this repository at a ref; use `./`"
        raise WorkflowReadingError(message)
    path = reference.removeprefix("./").removeprefix("$/")
    if not path.startswith(WORKFLOW_DIRECTORY):
        return None
    if "@" in path:
        message = f"{reference!r} is a local call carrying an `@ref`"
        raise WorkflowReadingError(message)
    return path.removeprefix(WORKFLOW_DIRECTORY)


def called_workflows(document: Document, repository: str) -> frozenset[str]:
    """Return the same-repository workflows one document's jobs call."""
    references = [job.get("uses") for job in jobs(document).values()]
    names = (
        local_callee(reference, repository)
        for reference in references
        if isinstance(reference, str)
    )
    return frozenset(name for name in names if name is not None)


def pull_request_closure(
    documents: dict[str, Document], repository: str
) -> dict[str, Document]:
    """Return every workflow a pull request can start, transitively.

    Raises
    ------
    WorkflowReadingError
        If no workflow serves a pull request, which is the reader failing
        rather than the repository complying, or a call names a workflow
        this tree does not hold.

    """
    seeds = [name for name, doc in documents.items() if is_pull_request_seed(doc)]
    if not seeds:
        message = "no workflow serves a pull request; the trigger reader is broken"
        raise WorkflowReadingError(message)
    return reachable(documents, seeds, repository)


def reachable(
    documents: dict[str, Document], seeds: list[str], repository: str
) -> dict[str, Document]:
    """Return the seed workflows and every local workflow they call, transitively.

    A called workflow runs in its caller's event context, so whatever a
    rule asks of the caller it must also ask of everything the caller
    reaches.

    Raises
    ------
    WorkflowReadingError
        If a call names a workflow this tree does not hold.

    """
    pending = list(seeds)
    found: dict[str, Document] = {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        if name not in documents:
            message = f"a workflow calls {name}, which does not exist"
            raise WorkflowReadingError(message)
        found[name] = documents[name]
        pending.extend(called_workflows(documents[name], repository))
    return found


def codescene_contacts(document: Document) -> list[str]:
    """Return every key or scalar in a document naming CodeScene.

    The whole document is read, case-folded, so neither a workflow-level
    `defaults.run.shell` nor a callee's secret declaration can reach the
    service unseen.

    Examples
    --------
    >>> codescene_contacts({"jobs": {"a": {"env": {"U": "https://API.CodeScene.io"}}}})
    ['https://API.CodeScene.io']

    """
    return [
        text
        for text in texts(document)
        if any(marker.search(text.casefold()) for marker in CODESCENE_MARKERS)
    ]


def inherited_secrets(document: Document) -> list[str]:
    """Return the jobs forwarding every secret with `secrets: inherit`.

    `inherit` names nothing, so a sweep for the credential's name cannot
    see what it hands over; a pull-request-reachable job may not use it.
    """
    return [
        name
        for name, job in jobs(document).items()
        if str(job.get("secrets", "")).strip() == "inherit"
    ]


def pull_request_violations(
    documents: dict[str, Document], repository: str
) -> list[str]:
    """Return every way the pull-request surface reaches CodeScene."""
    closure = pull_request_closure(documents, repository)
    return [
        f"{name}: {finding}"
        for name, document in sorted(closure.items())
        for finding in (
            *(f"names {text!r}" for text in codescene_contacts(document)),
            *(
                f"job {job} uses secrets: inherit"
                for job in inherited_secrets(document)
            ),
        )
    ]
