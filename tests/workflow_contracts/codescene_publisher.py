"""Rules for the one push-to-main workflow allowed to upload to CodeScene.

The publisher is found rather than named, as the only workflow in the tree
that contacts CodeScene at all, so a second uploader cannot hide behind
the first. Each rule returns its findings as text; an empty list is
compliance.
"""

from __future__ import annotations

import re
import typing as typ

from .codescene_reach import codescene_contacts
from .loading import Document, WorkflowReadingError
from .reading import jobs, steps, texts, trigger_filters, triggers

UPLOAD_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/upload-codescene-coverage"
)
COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
PINNED_COMMIT: typ.Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
REF_EXPRESSION: typ.Final[re.Pattern[str]] = re.compile(r"\$\{\{\s*github\.ref\s*\}\}")
EVENT_EXPRESSION: typ.Final[re.Pattern[str]] = re.compile(
    r"\$\{\{\s*github\.event_name\s*\}\}"
)
PERMITTED_TRIGGERS: typ.Final[frozenset[str]] = frozenset({"push", "workflow_dispatch"})


def find_publisher(documents: dict[str, Document]) -> tuple[str, Document]:
    """Return the single workflow that contacts CodeScene.

    Raises
    ------
    WorkflowReadingError
        If none does, or more than one does.

    """
    found = [name for name, doc in documents.items() if codescene_contacts(doc)]
    if len(found) != 1:
        message = f"exactly one workflow may contact CodeScene; found {found}"
        raise WorkflowReadingError(message)
    return found[0], documents[found[0]]


def action_steps(document: Document, action: str) -> list[dict[str, object]]:
    """Return every step in a document invoking one action at any ref."""
    return [
        step
        for job in jobs(document).values()
        for step in steps(job)
        if str(step.get("uses", "")).split("@", 1)[0] == action
    ]


def pin_of(step: dict[str, object]) -> str:
    """Return the ref after the `@` in a step's `uses:`."""
    return str(step.get("uses", "")).partition("@")[2]


def upload_step(document: Document) -> dict[str, object]:
    """Return the publisher's one upload step.

    Raises
    ------
    WorkflowReadingError
        If the uploader is invoked other than exactly once.

    """
    found = action_steps(document, UPLOAD_ACTION)
    if len(found) != 1:
        message = f"the publisher must upload exactly once; found {len(found)}"
        raise WorkflowReadingError(message)
    return found[0]


def trigger_violations(document: Document) -> list[str]:
    """Refuse any trigger but a push to main and an optional dispatch."""
    found = [
        f"trigger {name!r} is not permitted"
        for name in sorted(triggers(document) - PERMITTED_TRIGGERS)
    ]
    if "push" not in triggers(document):
        found.append("the publisher does not run on a push")
    if trigger_filters(document, "push") not in (
        {"branches": ["main"]},
        {"branches": "main"},
    ):
        found.append("the push trigger must filter on exactly `branches: [main]`")
    return found


def _cancels(concurrency: object) -> bool:
    """Return whether a concurrency declaration may cancel a running upload."""
    if not isinstance(concurrency, dict):
        return False
    return concurrency.get("cancel-in-progress", False) not in {False, "false"}


def upload_job(document: Document) -> dict[str, object]:
    """Return the job holding the publisher's one upload step."""
    step = upload_step(document)
    return next(
        job
        for job in jobs(document).values()
        if any(candidate is step for candidate in steps(job))
    )


def _group(concurrency: object) -> str:
    """Return a concurrency declaration's group, in either spelling."""
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    return str(group)


def _is_keyed(group: str) -> bool:
    """Return whether a group evaluates both the ref and the event name."""
    return bool(REF_EXPRESSION.search(group) and EVENT_EXPRESSION.search(group))


def _ref_keyed_violations(document: Document, governing: list[object]) -> list[str]:
    """Require a dispatchable publisher to key its group on the ref.

    GitHub keeps one pending run per group, so with a constant group a
    dispatch from a branch replaces a pending push to main; the dispatch
    then skips the guarded upload and that merge is never published.
    The event name is part of the key too: a dispatch on main does not
    advance the ratchet baseline, so it must not replace a pending push to
    main either. Every governing group must evaluate both, since a
    constant workflow group still collides whatever the job's group says,
    and the text `github.ref` outside an expression evaluates nothing.
    """
    present = [value for value in governing if value is not None]
    if "workflow_dispatch" not in triggers(document):
        return []
    return [
        f"concurrency group {_group(value)!r} is not keyed on the ref and event"
        for value in present
        if not _is_keyed(_group(value))
    ]


def concurrency_violations(document: Document) -> list[str]:
    """Require a concurrency group over the upload that never cancels.

    The group must sit on the workflow or on the job that uploads: one on
    an unrelated job leaves concurrent uploads possible. A newer push then
    replaces an older pending run rather than killing a running one, so
    the newest baseline wins and no upload is abandoned.
    """
    governing = [document.get("concurrency"), upload_job(document).get("concurrency")]
    found = (
        []
        if any(value is not None for value in governing)
        else ["neither the publisher nor its upload job declares a concurrency group"]
    )
    found += _ref_keyed_violations(document, governing)
    declared = [document.get("concurrency")] + [
        job.get("concurrency") for job in jobs(document).values()
    ]
    present = [value for value in declared if value is not None]
    return found + [
        f"concurrency {value!r} may cancel an upload"
        for value in present
        if _cancels(value)
    ]


def upload_step_violations(document: Document) -> list[str]:
    """Require the upload step's explicit mode and commit pin.

    The token, the guard and the availability check are the token module's
    rules; this one holds what the step asks the action to do.
    """
    step = upload_step(document)
    inputs = step.get("with") or {}
    if not isinstance(inputs, dict):
        return ["the upload step's `with` must be a mapping"]
    found = (
        []
        if inputs.get("mode") == "upload"
        else [f"mode is {inputs.get('mode')!r}, not 'upload'"]
    )
    if not PINNED_COMMIT.match(pin_of(step)):
        found.append(f"the uploader is not pinned to a commit: {step.get('uses')!r}")
    return found


def retired_checksum_violations(documents: dict[str, Document]) -> list[str]:
    """Refuse the retired installer checksum and its refresher anywhere."""
    found = [
        f"{name} names {text!r}"
        for name, document in sorted(documents.items())
        for text in texts(document)
        if re.search(r"installer-checksum|codescene_cli_sha256", text.casefold())
    ]
    return found + [
        f"{name} is the retired checksum refresher"
        for name in documents
        if name.startswith("get-codescene-sha.")
    ]
