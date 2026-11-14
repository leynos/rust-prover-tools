"""Validation of a rustup toolchain directory name.

A manifest records the toolchain by name, and the snapshot later joins that
name onto `rustup-home/toolchains`. The name must therefore be exactly one
plain path component, so it cannot escape the private rustup home or name a
sibling of it.
"""

from __future__ import annotations

import re
import typing as typ

__all__ = ("TOOLCHAIN_NAME_PATTERN", "is_safe_toolchain_name")

#: A rustup toolchain name is one plain path component. The length bound keeps
#: a hostile manifest from naming a component the filesystem cannot represent.
TOOLCHAIN_NAME_PATTERN: typ.Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)


def is_safe_toolchain_name(value: str) -> bool:
    """Return whether text names one plain rustup toolchain directory.

    Parameters
    ----------
    value : str
        Candidate toolchain identifier.

    Returns
    -------
    bool
        `True` only for a single component of letters, digits, dot, underscore
        and hyphen. An absolute path, a separator or a `..` component cannot
        escape `rustup-home/toolchains/<name>`.
    """
    if any(token in value for token in ("..", "/", "\\")):
        return False
    return TOOLCHAIN_NAME_PATTERN.fullmatch(value) is not None
