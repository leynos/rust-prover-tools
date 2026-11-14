"""Shared strict validators for snapshot pins and manifests.

These helpers fail closed on untrusted input. They never coerce a value: a
boolean is not an integer, a numeric string is not a number, and a truncated
digest is not padded. Error messages name the field and never echo private
payload.
"""

from __future__ import annotations

import math
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import REPOSITORY, SUPPORTED_TARGET

__all__ = (
    "FULL_SHA_PATTERN",
    "SHA256_PATTERN",
    "is_contained_relative_path",
    "require_full_sha",
    "require_int",
    "require_mapping",
    "require_non_negative_int",
    "require_number",
    "require_positive_int",
    "require_sequence",
    "require_sha256",
    "require_text",
    "require_truthy_text",
    "validate_repository",
    "validate_target",
)

FULL_SHA_PATTERN: typ.Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN: typ.Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


def _fail(message: str) -> typ.NoReturn:
    """Raise one user-facing validation failure."""
    raise ProverToolError(message)


def is_contained_relative_path(value: str) -> bool:
    """Return whether text names a file strictly below a root directory.

    Parameters
    ----------
    value : str
        Candidate relative path.

    Returns
    -------
    bool
        `True` only for a non-empty relative path with no empty component and
        no `..` component, so joining it can never leave the root.
    """
    parts = value.split("/")
    return not value.startswith("/") and ".." not in parts and "" not in parts


def require_mapping(value: object, field: str) -> dict[str, object]:
    """Return `value` when it is a JSON object.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    dict[str, object]
        The mapping, unchanged.

    Raises
    ------
    ProverToolError
        Raised when the value is not a string-keyed mapping.
    """
    if not isinstance(value, dict):
        _fail(f"snapshot {field} must be an object")
    return typ.cast("dict[str, object]", value)


def require_sequence(value: object, field: str) -> list[object]:
    """Return `value` when it is a JSON array.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    list[object]
        The array, unchanged.

    Raises
    ------
    ProverToolError
        Raised when the value is not a list.
    """
    if not isinstance(value, list):
        _fail(f"snapshot {field} must be an array")
    return typ.cast("list[object]", value)


def require_text(value: object, field: str) -> str:
    """Return `value` when it is a string, rejecting every other type.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    str
        The string, unchanged.

    Raises
    ------
    ProverToolError
        Raised when the value is not a string.
    """
    if not isinstance(value, str):
        _fail(f"snapshot {field} must be a string")
    if "\x00" in value:
        _fail(f"snapshot {field} must not contain NUL")
    return value


def require_truthy_text(value: object, field: str) -> str:
    """Return a non-empty string, rejecting blank and non-string values.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    str
        The non-empty string.

    Raises
    ------
    ProverToolError
        Raised when the value is not a non-empty string.
    """
    text = require_text(value, field)
    if not text.strip():
        _fail(f"snapshot {field} must not be empty")
    return text


def require_int(value: object, field: str) -> int:
    """Return `value` when it is an integer, not a boolean.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    int
        The integer, unchanged.

    Raises
    ------
    ProverToolError
        Raised when the value is not a true integer.
    """
    # `bool` is an `int` subclass, so an unchecked `isinstance(value, int)`
    # would accept `true` as schema version 1.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"snapshot {field} must be an integer")
    return value


def require_non_negative_int(value: object, field: str) -> int:
    """Return a non-negative integer.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    int
        The non-negative integer.

    Raises
    ------
    ProverToolError
        Raised when the value is not a non-negative integer.
    """
    number = require_int(value, field)
    if number < 0:
        _fail(f"snapshot {field} must not be negative")
    return number


def require_number(value: object, field: str) -> float:
    """Return a finite real number, rejecting booleans and non-numbers.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    float
        The value as a float.

    Raises
    ------
    ProverToolError
        Raised when the value is not a finite integer or float.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"snapshot {field} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        # A JSON integer can exceed the float range; that is a finite-looking
        # input that cannot be recorded as a measurement.
        msg = f"snapshot {field} must be finite"
        raise ProverToolError(msg) from exc
    if not math.isfinite(number):
        _fail(f"snapshot {field} must be finite")
    return number


def require_positive_int(value: object, field: str) -> int:
    """Return a strictly positive integer.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    int
        The positive integer.

    Raises
    ------
    ProverToolError
        Raised when the value is not a positive integer.
    """
    number = require_int(value, field)
    if number < 1:
        _fail(f"snapshot {field} must be positive")
    return number


def require_full_sha(value: object, field: str) -> str:
    """Return a full lowercase 40-hex Git SHA.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    str
        The validated commit.

    Raises
    ------
    ProverToolError
        Raised when the value is not a full lowercase SHA.
    """
    text = require_text(value, field)
    if FULL_SHA_PATTERN.fullmatch(text) is None:
        _fail(f"snapshot {field} must be a full lowercase 40-hex commit")
    return text


def require_sha256(value: object, field: str) -> str:
    """Return a lowercase 64-hex SHA-256 digest.

    Parameters
    ----------
    value : object
        Untrusted value.
    field : str
        Field name used in the error message.

    Returns
    -------
    str
        The validated digest.

    Raises
    ------
    ProverToolError
        Raised when the value is not a full lowercase digest.
    """
    text = require_text(value, field)
    if SHA256_PATTERN.fullmatch(text) is None:
        _fail(f"snapshot {field} must be a lowercase 64-hex SHA-256 digest")
    return text


def validate_repository(value: object) -> str:
    """Return the one supported publishing repository.

    Parameters
    ----------
    value : object
        Untrusted value.

    Returns
    -------
    str
        The validated repository.

    Raises
    ------
    ProverToolError
        Raised when the value differs from `REPOSITORY`.
    """
    text = require_text(value, "repository")
    if text != REPOSITORY:
        _fail(f"snapshot repository must be {REPOSITORY}")
    return text


def validate_target(value: object) -> str:
    """Return the one supported target triple.

    Parameters
    ----------
    value : object
        Untrusted value.

    Returns
    -------
    str
        The validated target.

    Raises
    ------
    ProverToolError
        Raised when the target is unsupported.
    """
    text = require_text(value, "target")
    if text != SUPPORTED_TARGET:
        _fail(f"snapshot target must be {SUPPORTED_TARGET}")
    return text
