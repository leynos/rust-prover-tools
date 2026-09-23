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
from .expressions import ConditionError, missing_terms
from .loading import Document, WorkflowReadingError
from .reading import jobs, steps, texts, trigger_filters, triggers

UPLOAD_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/upload-codescene-coverage"
)
COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
PINNED_COMMIT: typ.Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
CS_BINDING: typ.Final[str] = "${{ secrets.CS_ACCESS_TOKEN }}"
CS_INPUT: typ.Final[str] = "${{ env.CS_ACCESS_TOKEN }}"
MAIN_REF_GUARD: typ.Final[str] = "github.ref == 'refs/heads/main'"
CS_GUARD: typ.Final[str] = "env.CS_ACCESS_TOKEN != ''"
UPLOAD_GUARD: typ.Final[frozenset[str]] = frozenset({MAIN_REF_GUARD, CS_GUARD})
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
    declared = [document.get("concurrency")] + [
        job.get("concurrency") for job in jobs(document).values()
    ]
    present = [value for value in declared if value is not None]
    return found + [
        f"concurrency {value!r} may cancel an upload"
        for value in present
        if _cancels(value)
    ]


def _guard_violations(step: dict[str, object]) -> list[str]:
    """Require the ref and token guard as whole `&&` terms."""
    try:
        missing = missing_terms(step.get("if"), UPLOAD_GUARD)
    except ConditionError as error:
        return [str(error)]
    return [f"the upload guard lacks {term!r}" for term in missing]


def upload_step_violations(document: Document) -> list[str]:
    """Require the upload step's mode, pin, guard and positive token binding.

    A guard on `env.CS_ACCESS_TOKEN != ''` alone passes with the binding
    deleted, because the missing variable reads as empty and the upload
    then skips for ever; so the binding and the input are asserted.
    """
    step = upload_step(document)
    inputs = step.get("with") or {}
    environment = step.get("env") or {}
    if not isinstance(inputs, dict) or not isinstance(environment, dict):
        return ["the upload step's `with` and `env` must be mappings"]
    expected = {
        "mode": ("upload", inputs.get("mode")),
        "access-token": (CS_INPUT, inputs.get("access-token")),
        "env CS_ACCESS_TOKEN": (CS_BINDING, environment.get("CS_ACCESS_TOKEN")),
    }
    found = [
        f"{name} is {actual!r}, not {wanted!r}"
        for name, (wanted, actual) in expected.items()
        if actual != wanted
    ]
    if not PINNED_COMMIT.match(pin_of(step)):
        found.append(f"the uploader is not pinned to a commit: {step.get('uses')!r}")
    return found + _guard_violations(step)


def token_scope_violations(document: Document) -> list[str]:
    """Refuse the credential anywhere in the publisher but the upload step."""
    step = upload_step(document)
    elsewhere = {key: value for key, value in document.items() if key != "jobs"}
    rest = (
        [elsewhere]
        + [
            {key: value for key, value in job.items() if key != "steps"}
            for job in jobs(document).values()
        ]
        + [
            other
            for job in jobs(document).values()
            for other in steps(job)
            if other is not step
        ]
    )
    return [
        f"the credential appears outside the upload step: {text!r}"
        for part in rest
        for text in texts(part)
        if "cs_access_token" in text.casefold()
    ]


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
