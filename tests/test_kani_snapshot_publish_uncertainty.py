"""What a publication reports once its publish call has been entered.

A publish call that has been entered is irreversible in effect even when it
fails: the release may already be public. These tests hold every way that can
happen after the write — a permanent read, a permanent verification, a
permanent failure of the publish call itself, and a transient failure followed
by a permanent one — and require each to name the uncertainty, keep the
original error as its cause, state the warning once, and never send the write
again.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish import publish_release
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseTransientError,
    VerifiedAssets,
)
from tests import kani_publish_fixtures as fixtures
from tests.kani_release_api_fixtures import FakeReleaseApi

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib


class _UnreadableAfterPublishApi(FakeReleaseApi):
    """Refuse every observation after the publish, without retrying.

    A permanent read failure is not the retryable kind the loop tolerates: it
    ends the run. What it must not do is hide that the release is already
    public.
    """

    def __init__(self) -> None:
        """Create the fake with its release still readable."""
        super().__init__()
        self.published = False

    def publish(self, tag: str, release_id: int) -> None:
        """Publish, then make the release unreadable from that moment on."""
        super().publish(tag, release_id)
        self.published = True

    def observe(self, tag: str) -> ObservedRelease:
        """Fail permanently once the release has been published."""
        if self.published:
            msg = "the release could not be read"
            raise ProverToolError(msg)
        return super().observe(tag)


def test_a_permanent_read_failure_after_the_publish_still_names_the_write(
    tmp_path: pathlib.Path,
) -> None:
    """A failed confirmation keeps its diagnosis and reports the uncertainty.

    The write landed, the run cannot see it, and the read error is the only
    detail it holds. Reporting that alone would invite the retry the run has
    just made unsafe, so the read error is kept as the cause and the message
    says the release may already be public.
    """
    api = _UnreadableAfterPublishApi()
    with pytest.raises(ProverToolError, match="may already be public") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=4)
    assert "could not be read" in str(raised.value), "the read error was lost"
    assert isinstance(raised.value.__cause__, ProverToolError), (
        "the original failure was not kept as the cause"
    )
    assert api.calls.count("publish") == 1, "publication was attempted twice"


class _UnverifiableAfterPublishApi(FakeReleaseApi):
    """Fail the verification that follows the publish.

    A published release is read as unverified, so the next decision asks for
    another verification. That download is the step after the write, and its
    failure must not read as though nothing was published.
    """

    def __init__(self) -> None:
        """Create the fake with its release still unverified."""
        super().__init__()
        self.published = False

    def publish(self, tag: str, release_id: int) -> None:
        """Publish, then refuse every later verification."""
        super().publish(tag, release_id)
        self.published = True

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Fail permanently once the release has been published."""
        if self.published:
            msg = "the published bytes could not be authenticated"
            raise ProverToolError(msg)
        return super().verify(tag, observed, names)


def test_a_failed_verification_after_the_publish_still_names_the_write(
    tmp_path: pathlib.Path,
) -> None:
    """A step after the write can fail, and the ending must still say so.

    The budget reaches the step after the publish, where the published release
    asks to be verified again. The verification fails permanently, so the run
    ends there; the error must report both that verification failed and that
    the release may already be public.
    """
    api = _UnverifiableAfterPublishApi()
    with pytest.raises(ProverToolError, match="may already be public") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=5)
    assert "could not be authenticated" in str(raised.value), (
        "the verification error was lost"
    )
    assert isinstance(raised.value.__cause__, ProverToolError), (
        "the original failure was not kept as the cause"
    )
    assert api.calls.count("publish") == 1, "publication was attempted twice"


class _PermanentPublishFailureApi(FakeReleaseApi):
    """Fail the publish call itself, permanently, once the call is entered.

    A connection that closes after the PATCH was sent looks like this: not a
    timeout, so the transport does not call it retryable, but the write may
    still have landed.
    """

    def publish(self, tag: str, release_id: int) -> None:
        """Record the call, then fail as a permanent error would."""
        self.calls.append("publish")
        msg = "the connection was closed before the response was read"
        raise ProverToolError(msg)


def test_a_permanent_publish_failure_still_names_the_write(
    tmp_path: pathlib.Path,
) -> None:
    """A failed publish call cannot deny the write it may have made.

    The call was entered, so its failure is not evidence that nothing happened.
    Reporting the bare error would invite a retry that publishes twice, so the
    message must carry the uncertainty and keep the diagnosis as its cause.
    """
    api = _PermanentPublishFailureApi()
    with pytest.raises(ProverToolError, match="may already be public") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=6)
    assert "connection was closed" in str(raised.value), "the publish error was lost"
    assert isinstance(raised.value.__cause__, ProverToolError), (
        "the original failure was not kept as the cause"
    )
    assert api.calls.count("publish") == 1, "publication was attempted twice"


class _RetriedPublishFailureApi(FakeReleaseApi):
    """Fail the first publish transiently and the second permanently.

    The first failure may have landed, so the run observes and tries again; the
    second can still happen after its own write was entered. Both are the same
    uncertainty, and the warning must be stated once rather than layered twice.
    """

    def __init__(self) -> None:
        """Create the fake with no publish attempted yet."""
        super().__init__()
        self.attempts = 0

    def publish(self, tag: str, release_id: int) -> None:
        """Fail transiently once, then permanently, without publishing."""
        self.calls.append("publish")
        self.attempts += 1
        if self.attempts == 1:
            msg = "the publish response timed out"
            raise ReleaseTransientError(msg)
        msg = "the publish response could not be read"
        raise ProverToolError(msg)


def test_a_retried_publish_failure_warns_once(
    tmp_path: pathlib.Path,
) -> None:
    """Two entered publish calls are one uncertainty, stated once.

    A transient failure followed by a permanent one must not nest the warning
    inside itself: the operator needs one sentence naming the uncertainty and
    the failure that produced it.
    """
    api = _RetriedPublishFailureApi()
    with pytest.raises(ProverToolError, match="may already be public") as raised:
        publish_release(fixtures.request(tmp_path), api, max_steps=8)
    message = str(raised.value)
    assert message.count("may already be public") == 1, (
        f"the uncertainty was stated more than once: {message!r}"
    )
    assert "could not be read" in message, "the final publish error was lost"
    assert isinstance(raised.value.__cause__, ProverToolError), (
        "the original failure was not kept as the cause"
    )
    assert api.calls.count("publish") == 2, "the retry count changed"
