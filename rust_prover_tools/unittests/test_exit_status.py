"""Contracts for the shutdown-flush guard."""

from __future__ import annotations

import io
import os
import typing as typ

from rust_prover_tools import exit_status

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest

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


class _BrokenPipeStream(io.StringIO):
    """A stream whose writes fail as a closed pipe does."""

    def write(self, _text: str) -> int:
        """Fail as a closed pipe does."""
        raise BrokenPipeError


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


def _installed_shutdown_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Callable[[], None]:
    """Install the guard and return the shutdown hook it registered.

    The guard's behaviour is only reachable through `atexit`, so each test
    captures the hook from there and runs it directly; that keeps the tests on
    the public entry point rather than on the helpers it installs.
    """
    hooks: list[cabc.Callable[[], None]] = []
    monkeypatch.setattr(exit_status.atexit, "register", hooks.append)
    exit_status.guard_shutdown_flush()
    assert len(hooks) == 1, "the guard should register exactly one shutdown hook"
    return hooks[0]


def test_closed_stream_is_redirected_to_the_null_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed flush moves the descriptor so the interpreter's flush succeeds."""
    target = tmp_path / "pipe-buffer"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    stream = _ClosingStream(descriptor)
    monkeypatch.setattr(exit_status.sys, "stdout", stream)
    monkeypatch.setattr(exit_status.sys, "stderr", _OpenStream())
    try:
        _installed_shutdown_hook(monkeypatch)()
        os.write(descriptor, b"discarded")
    finally:
        os.close(descriptor)

    assert stream.flush_calls == 1, "the guard should probe the stream once"
    assert target.read_text(encoding="utf-8") == "", (
        "the redirected descriptor should discard writes instead of reaching "
        "the closed pipe"
    )


def test_healthy_stream_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that flushes is not redirected."""
    stream = _OpenStream()
    monkeypatch.setattr(exit_status.sys, "stdout", stream)
    monkeypatch.setattr(exit_status.sys, "stderr", _OpenStream())

    _installed_shutdown_hook(monkeypatch)()

    assert stream.flush_calls == 1, "the guard should flush a healthy stream"


def test_missing_stream_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A windowed interpreter has nothing to flush and nothing to redirect."""
    monkeypatch.setattr(exit_status.sys, "stdout", None)
    monkeypatch.setattr(exit_status.sys, "stderr", None)

    _installed_shutdown_hook(monkeypatch)()


def test_descriptorless_closed_stream_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed stream without a descriptor cannot fail a later flush."""
    stream = _ClosingStream(descriptor=None)
    monkeypatch.setattr(exit_status.sys, "stdout", stream)
    monkeypatch.setattr(exit_status.sys, "stderr", _OpenStream())

    _installed_shutdown_hook(monkeypatch)()

    assert stream.flush_calls == 1, "the guard should still probe the stream"


def test_both_output_streams_are_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A combined pipe leaves both streams dead, so both are redirected."""
    stdout = _ClosingStream(descriptor=None)
    stderr = _ClosingStream(descriptor=None)
    monkeypatch.setattr(exit_status.sys, "stdout", stdout)
    monkeypatch.setattr(exit_status.sys, "stderr", stderr)

    _installed_shutdown_hook(monkeypatch)()

    assert (stdout.flush_calls, stderr.flush_calls) == (1, 1), (
        "the guard should probe standard output and standard error"
    )


def test_print_lines_writes_every_line() -> None:
    """A healthy stream receives each line followed by a newline."""
    stream = io.StringIO()

    exit_status.print_lines(("first", "second"), stream=stream)

    assert stream.getvalue() == "first\nsecond\n", (
        "the helper should print each line on its own line"
    )


def test_print_lines_defaults_to_the_current_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standard output redirected after import is still honoured."""
    stream = io.StringIO()
    monkeypatch.setattr(exit_status.sys, "stdout", stream)

    exit_status.print_lines(("first",))

    assert stream.getvalue() == "first\n", (
        "the helper should resolve standard output when it runs"
    )


def test_print_lines_ignores_a_closed_pipe() -> None:
    """A dead reader cannot turn completed work into a failure."""
    stream = _BrokenPipeStream()

    exit_status.print_lines(("first", "second"), stream=stream)

    assert stream.getvalue() == "", "a broken pipe accepts no output"


def test_guard_registers_the_shutdown_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public entry point registers the redirect at interpreter shutdown."""
    registered: list[cabc.Callable[[], None]] = []
    monkeypatch.setattr(exit_status.atexit, "register", registered.append)

    exit_status.guard_shutdown_flush()

    assert len(registered) == 1, "the guard should register exactly one hook"
    assert callable(registered[0]), "the registered hook should be callable"
