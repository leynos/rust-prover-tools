"""Contracts for the shutdown-flush guard."""

from __future__ import annotations

import io
import os
import typing as typ

import pytest

from rust_prover_tools import exit_status

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: Raised by doubles that model a stream without a file descriptor.
_NO_DESCRIPTOR_MESSAGE = "stream has no usable descriptor"


class _UnexpectedRedirectError(RuntimeError):
    """Raised when the guard redirects a stream that flushed successfully."""

    def __init__(self) -> None:
        """Describe the redirect that should not have happened."""
        super().__init__("a stream that flushed must not be redirected")


class _ClosingStream(io.StringIO):
    """A standard-output double whose flush fails.

    A closed pipe raises `BrokenPipeError` from both `write` and `flush`; this
    double raises from `flush`, which is the call the guard makes before the
    interpreter's own final flush.
    """

    def __init__(self, descriptor: int | None) -> None:
        """Create a stream reporting ``descriptor`` from `fileno`."""
        super().__init__()
        self._descriptor = descriptor
        self.flush_calls = 0

    def flush(self) -> None:
        """Fail as a closed pipe does, recording the attempt."""
        self.flush_calls += 1
        raise BrokenPipeError

    def fileno(self) -> int:
        """Return the configured descriptor, or fail as an unbuffered sink does."""
        if self._descriptor is None:
            raise io.UnsupportedOperation(_NO_DESCRIPTOR_MESSAGE)
        return self._descriptor


class _OpenStream(io.StringIO):
    """A standard-output double that flushes normally."""

    def __init__(self) -> None:
        """Create a healthy stream."""
        super().__init__()
        self.flush_calls = 0

    def flush(self) -> None:
        """Record the flush without failing."""
        self.flush_calls += 1
        super().flush()

    def fileno(self) -> int:
        """Fail loudly if the guard asks a healthy stream for its descriptor."""
        raise _UnexpectedRedirectError


def test_closed_stdout_is_redirected_to_the_null_device(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed flush moves the descriptor so the interpreter's flush succeeds."""
    target = tmp_path / "pipe-buffer"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    stream = _ClosingStream(descriptor)
    monkeypatch.setattr(exit_status.sys, "stdout", stream)
    try:
        exit_status._redirect_closed_stdout()
        os.write(descriptor, b"discarded")
    finally:
        os.close(descriptor)

    assert stream.flush_calls == 1, "the guard should probe the stream once"
    assert target.read_text(encoding="utf-8") == "", (
        "the redirected descriptor should discard writes instead of reaching "
        "the closed pipe"
    )


def test_healthy_stdout_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that flushes is not redirected."""
    stream = _OpenStream()
    monkeypatch.setattr(exit_status.sys, "stdout", stream)

    exit_status._redirect_closed_stdout()

    assert stream.flush_calls == 1, "the guard should flush a healthy stream"


@pytest.mark.parametrize("stream", [None])
def test_missing_stdout_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    stream: None,
) -> None:
    """A windowed interpreter has nothing to flush and nothing to redirect."""
    monkeypatch.setattr(exit_status.sys, "stdout", stream)

    exit_status._redirect_closed_stdout()


def test_descriptorless_closed_stdout_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed stream without a descriptor cannot fail a later flush."""
    stream = _ClosingStream(descriptor=None)
    monkeypatch.setattr(exit_status.sys, "stdout", stream)

    exit_status._redirect_closed_stdout()

    assert stream.flush_calls == 1, "the guard should still probe the stream"


def test_guard_registers_the_shutdown_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public entry point registers the redirect at interpreter shutdown."""
    registered: list[cabc.Callable[[], None]] = []
    monkeypatch.setattr(exit_status.atexit, "register", registered.append)

    exit_status.guard_shutdown_flush()

    assert registered == [exit_status._redirect_closed_stdout], (
        "the guard should register exactly one shutdown hook"
    )
