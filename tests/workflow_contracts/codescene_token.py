"""Rules for how the publisher learns of the credential and hands it over.

The uploader is a composite action, and a composite passes its step's
`env` to every nested step, so a token bound there reaches the cache and
artefact actions inside it too. The credential is therefore handed over
only through the uploader's `access-token` input, and its presence is
learnt from a separate step whose one command writes the answer to an
output without binding anything:
`echo "available=${{ secrets.CS_ACCESS_TOKEN != '' }}" >> "$GITHUB_OUTPUT"`.
The expression is evaluated before the shell runs, so the step holds no
secret and has no shell conditional for a rewrite to hide behind.
"""

from __future__ import annotations

import typing as typ

from .codescene_publisher import upload_job, upload_step
from .expressions import ConditionError, missing_terms
from .loading import WorkflowReadingError
from .reading import jobs, steps, texts

if typ.TYPE_CHECKING:
    from .loading import Document

#: The one command the availability check may run.
CHECK_COMMAND: typ.Final[str] = (
    'echo "available=${{ secrets.CS_ACCESS_TOKEN != \'\' }}" >> "$GITHUB_OUTPUT"'
)
#: The only way the credential may reach the uploader.
CREDENTIAL_INPUT: typ.Final[str] = "${{ secrets.CS_ACCESS_TOKEN }}"
MAIN_REF_GUARD: typ.Final[str] = "github.ref == 'refs/heads/main'"


def check_step(document: Document) -> dict[str, object]:
    """Return the publisher's one availability check.

    Raises
    ------
    WorkflowReadingError
        If no step, or more than one, runs the check command; without it
        the upload's guard can never be true and the upload skips for ever.

    """
    found = [
        step
        for job in jobs(document).values()
        for step in steps(job)
        if str(step.get("run", "")).strip() == CHECK_COMMAND
    ]
    if len(found) != 1:
        message = f"the publisher must check the token exactly once; found {len(found)}"
        raise WorkflowReadingError(message)
    return found[0]


def availability_term(document: Document) -> str:
    """Return the guard term that reads the check step's output."""
    return f"steps.{check_step(document).get('id')}.outputs.available == 'true'"


def check_step_violations(document: Document) -> list[str]:
    """Require the check to run unconditionally, bind nothing, and come first.

    It must be named by an `id` so its output can be read, sit in the
    upload job before the upload, and carry no `if:` or `env`.
    """
    step = check_step(document)
    positions = {
        id(candidate): index
        for index, candidate in enumerate(steps(upload_job(document)))
    }
    found = [] if isinstance(step.get("id"), str) else ["the check step has no id"]
    found += [
        f"the check step declares {key!r}" for key in ("if", "env") if key in step
    ]
    is_first = (
        positions.get(id(step), len(positions)) < positions[id(upload_step(document))]
    )
    return found + (
        [] if is_first else ["the check must precede the upload in its job"]
    )


def upload_guard_violations(document: Document) -> list[str]:
    """Require the ref guard and the check's output as whole `&&` terms."""
    required = frozenset({MAIN_REF_GUARD, availability_term(document)})
    try:
        missing = missing_terms(upload_step(document).get("if"), required)
    except ConditionError as error:
        return [str(error)]
    return [f"the upload guard lacks {term!r}" for term in missing]


def upload_token_violations(document: Document) -> list[str]:
    """Require the uploader's `access-token` to read the secret directly."""
    inputs = upload_step(document).get("with") or {}
    actual = inputs.get("access-token") if isinstance(inputs, dict) else None
    if actual == CREDENTIAL_INPUT:
        return []
    return [f"access-token is {actual!r}, not {CREDENTIAL_INPUT!r}"]


def token_scope_violations(document: Document) -> list[str]:
    """Refuse the credential anywhere but the check command and the input.

    Every other key or value naming it is refused, the upload step's own
    `env` and every other step's included.
    """
    allowed = {id(check_step(document)), id(upload_step(document))}
    upload = upload_step(document)
    kept = {key: value for key, value in upload.items() if key != "with"}
    upload_inputs = upload.get("with") or {}
    other_inputs = (
        {k: v for k, v in upload_inputs.items() if k != "access-token"}
        if isinstance(upload_inputs, dict)
        else upload_inputs
    )
    rest: list[object] = [
        {key: value for key, value in document.items() if key != "jobs"},
        *(
            {key: value for key, value in job.items() if key != "steps"}
            for job in jobs(document).values()
        ),
        *(
            other
            for job in jobs(document).values()
            for other in steps(job)
            if id(other) not in allowed
        ),
        kept,
        other_inputs,
        {k: v for k, v in check_step(document).items() if k != "run"},
    ]
    return [
        f"the credential appears outside the check and the input: {text!r}"
        for part in rest
        for text in texts(part)
        if "cs_access_token" in text.casefold()
    ]
