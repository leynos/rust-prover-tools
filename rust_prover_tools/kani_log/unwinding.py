"""Decide whether a block's solver arguments kept unwinding checks on.

Kani's unwinding checks are on by default, and the echoed solver command is
the only evidence of whether a run disabled them. This lives apart from the log
extractor so the extractor stays within the module-length limit.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.syntax import (
    NO_UNWINDING_ASSERTIONS_FLAG,
    SOLVER_FRAME,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.model import HarnessBlock

__all__ = ("unwinding_checks_enabled",)


def unwinding_checks_enabled(block: HarnessBlock) -> bool | None:
    """Report whether the solver was asked to generate unwinding assertions.

    Parameters
    ----------
    block : HarnessBlock
        One parsed harness block.

    Returns
    -------
    bool | None
        ``False`` when the echoed command disables the assertions, ``True``
        otherwise, and ``None`` when no solver command was echoed or its
        arguments cannot be read.  An unreadable command may still carry the
        disabling flag, so it must not be read as evidence that the checks
        were generated.

    Notes
    -----
    The rule is Kani's, read from the pinned driver rather than guessed: the
    driver passes ``--no-unwinding-assertions`` when the checks are off and
    otherwise passes only ``--no-self-loops-to-assumptions``, relying on
    CBMC's own default.  So the echoed command decides with one flag, and a
    missing bound says nothing about whether the assertions were generated.

    """
    if block.solver_command is None:
        return None
    arguments = _solver_arguments(block.solver_command)
    if arguments is None:
        return None
    return NO_UNWINDING_ASSERTIONS_FLAG not in arguments


def _solver_arguments(command: str) -> frozenset[str] | None:
    """Split the echoed ``cbmc`` command into its arguments and their options.

    The echo is read at Kani's own boundaries rather than as shell text.  Kani
    wraps an argument in double quotes when, and only when, it holds a space,
    and escapes nothing: an apostrophe is an ordinary character.  Splitting it
    as shell words would let one apostrophe open a quoted region spanning the
    arguments that follow, which hides a flag such as
    ``--no-unwinding-assertions`` from the membership test and reports checks
    that were disabled as enabled.

    Each argument contributes itself and, when written ``--option=value``, the
    option alone; the flags read here are written as separate words, so
    matching whole arguments is enough.

    The echo's own framing is removed first.  A closing backtick would
    otherwise join the last argument and hide a trailing flag.

    A wrapped argument is only recoverable when it ends the echo.  Because
    Kani escapes nothing inside it, a quote followed by more text can close
    the argument or be a quote the value contains; no fact in the rendering
    says which.  Reading it as a boundary would let one rendering stand for
    two argument lists, and the flag the other reading contains would go
    unread.

    Returns
    -------
    frozenset[str] | None
        The arguments and their option names, or ``None`` when the rendering
        cannot be split.  An unterminated wrap, text after a closing quote, or
        a wrapped argument followed by more arguments means the boundaries
        cannot be recovered; none of those shapes may be read as evidence that
        the checks were generated.
    """
    if not command.startswith(SOLVER_FRAME) or not command.endswith("`"):
        # A capture that cut the echo mid-argument leaves a flag that is
        # not a whole word, so none of its options can be read as fact.
        return None
    framed = command[len(SOLVER_FRAME) : -1]
    words = _rendered_words(framed)
    if words is None:
        return None
    return frozenset(word for item in words for word in (item, item.partition("=")[0]))


def _rendered_words(rendered: str) -> list[str] | None:
    """Split a command echo at the boundaries Kani rendered it with.

    Parameters
    ----------
    rendered : str
        The echoed command without its framing backticks.

    Returns
    -------
    list[str] | None
        The words Kani passed, or ``None`` when a quote leaves them ambiguous.

    """
    words: list[str] = []
    index = 0
    end_of_rendering = len(rendered)
    while index < end_of_rendering:
        if rendered[index] == " ":
            index += 1
            continue
        if rendered[index] == '"':
            closing = rendered.find('"', index + 1)
            if closing == -1:
                return None
            words.append(rendered[index + 1 : closing])
            following = closing + 1
            while following < end_of_rendering and rendered[following] == " ":
                following += 1
            if following < end_of_rendering:
                # Kani escapes nothing inside a wrapped argument, so a quote
                # followed by more text is ambiguous: it can close the
                # argument, or it can be a quote the value itself contains
                # with the rest of the value continuing after the space.  No
                # fact in the rendering distinguishes the two reads, so the
                # boundary is unknown rather than assumed.
                return None
            index = following
            continue
        end = rendered.find(" ", index)
        if end == -1:
            end = end_of_rendering
        words.append(rendered[index:end])
        index = end
    return words
