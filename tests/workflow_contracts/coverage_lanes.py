"""Rules for the coverage lanes: pull requests ratchet, main writes the baseline.

generate-coverage saves its ratchet baseline on a push to main, so the
publisher must be the only lane that measures on that event, and every
pull-request lane must measure what the publisher measures: a lane
compiling a different selection compares a feature difference, not a
commit difference, and a number comes out either way.
"""

from __future__ import annotations

import typing as typ

from .codescene_publisher import (
    COVERAGE_ACTION,
    PINNED_COMMIT,
    UPLOAD_ACTION,
    action_steps,
    pin_of,
    upload_step,
)
from .codescene_reach import reachable
from .expressions import ConditionError, missing_terms
from .reading import triggers

if typ.TYPE_CHECKING:
    from .loading import Document

#: Inputs that may differ between a pull-request lane and the publisher,
#: because they name or ship the report rather than select what runs.
LANE_LOCAL_INPUTS: typ.Final[frozenset[str]] = frozenset({
    "artefact-name-suffix",
    "publish-artefact",
})
PULL_REQUEST_GUARD: typ.Final[frozenset[str]] = frozenset({
    "github.event_name == 'pull_request'",
})


def _inputs(step: dict[str, object]) -> dict[str, object]:
    """Return a step's `with` mapping, empty when it declares none."""
    inputs = step.get("with") or {}
    return typ.cast("dict[str, object]", inputs) if isinstance(inputs, dict) else {}


def _is_true(value: object) -> bool:
    """Return whether an action input reads as true."""
    return value is True or value == "true"


def _is_false(value: object) -> bool:
    """Return whether an action input reads as false."""
    return value is False or value == "false"


def pull_request_lane_violations(closure: dict[str, Document]) -> list[str]:
    """Require every pull-request lane to ratchet and publish nothing."""
    lanes = [
        (name, step)
        for name, document in sorted(closure.items())
        for step in action_steps(document, COVERAGE_ACTION)
    ]
    if not lanes:
        return ["no pull-request workflow generates coverage"]
    return [
        f"{name}: generate-coverage must set {key}: {wanted!r}"
        for name, step in lanes
        for key, wanted, check in (
            ("with-ratchet", "true", _is_true),
            ("publish-artefact", "false", _is_false),
        )
        if not check(_inputs(step).get(key))
    ]


def _guarded_to_pull_requests(step: dict[str, object]) -> bool:
    """Return whether a step runs only for a pull request."""
    try:
        return not missing_terms(step.get("if"), PULL_REQUEST_GUARD)
    except ConditionError:
        return False


def second_writer_violations(
    documents: dict[str, Document], publisher: str, repository: str
) -> list[str]:
    """Refuse coverage on a push anywhere but the publisher.

    A lane running on both events would write a second baseline on every
    push to main, so each generate-coverage step that another push-started
    workflow reaches, itself or through a local reusable workflow it
    calls, must run for pull requests only.
    """
    seeds = [
        name
        for name, document in documents.items()
        if name != publisher and "push" in triggers(document)
    ]
    closure = reachable(documents, seeds, repository)
    return [
        f"{name}: generate-coverage can run on a push; guard it to pull requests"
        for name, document in sorted(closure.items())
        if name != publisher
        for step in action_steps(document, COVERAGE_ACTION)
        if not _guarded_to_pull_requests(step)
    ]


def _selection(step: dict[str, object]) -> dict[str, object]:
    """Return the inputs that decide what a coverage run measures."""
    return {
        key: value
        for key, value in _inputs(step).items()
        if key not in LANE_LOCAL_INPUTS
    }


def publisher_lane_violations(
    publisher: Document, closure: dict[str, Document]
) -> list[str]:
    """Require the publisher to ratchet the selection every lane measures.

    The publisher's generator, its uploader and every pull-request
    generator share one commit pin, so the lanes measure with the same
    action that writes their baseline.
    """
    generators = action_steps(publisher, COVERAGE_ACTION)
    if len(generators) != 1:
        return [f"the publisher must generate coverage once; found {len(generators)}"]
    baseline = generators[0]
    found = (
        []
        if _is_true(_inputs(baseline).get("with-ratchet"))
        else ["the publisher's generate-coverage must set with-ratchet: 'true'"]
    )
    pins = {pin_of(baseline), pin_of(upload_step(publisher))}
    for name, document in sorted(closure.items()):
        for step in action_steps(document, COVERAGE_ACTION):
            pins.add(pin_of(step))
            if _selection(step) != _selection(baseline):
                found.append(f"{name}: coverage selection differs from the publisher's")
    if len(pins) != 1 or not all(PINNED_COMMIT.match(pin) for pin in pins):
        found.append(
            f"{COVERAGE_ACTION} and {UPLOAD_ACTION} must share one commit pin: "
            f"{sorted(pins)}"
        )
    return found


def report_violations(document: Document) -> list[str]:
    """Require the uploader to read the report the generator writes.

    An uploader naming another path or format finds no report, or the
    wrong one, and CodeScene keeps showing older coverage while every
    other rule passes.
    """
    generators = action_steps(document, COVERAGE_ACTION)
    if len(generators) != 1:
        return [f"the publisher must generate coverage once; found {len(generators)}"]
    written, read = _inputs(generators[0]), _inputs(upload_step(document))
    return [
        f"the uploader's {reads!r} is {read.get(reads)!r}, "
        f"but the generator's {writes!r} is {written.get(writes)!r}"
        for writes, reads in (("output-path", "path"), ("format", "format"))
        if read.get(reads) != written.get(writes)
    ]
