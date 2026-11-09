"""Strict JSON decoding for untrusted snapshot documents.

Python's default decoder accepts duplicate object keys (last one wins) and the
non-finite extensions `NaN`/`Infinity`. A manifest whose parsed meaning depends
on those leniencies could pass review in one form and verify in another, so
both are refused here. Container nesting is also bounded explicitly, because
the decoder's own recursion limit varies with the interpreter build and test
instrumentation.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("strict_json_loads",)

#: Containers a snapshot document may nest. Real manifests nest a handful of
#: objects and arrays, so a fixed ceiling is ample. Enforcing it explicitly
#: keeps hostile depth a deterministic parse error: the decoder's own
#: `RecursionError` threshold moves with the interpreter build and with
#: coverage instrumentation.
_MAX_NESTING_DEPTH = 32


def _reject_constant(name: str) -> typ.NoReturn:
    """Reject JSON's non-finite numeric extensions."""
    msg = f"snapshot manifest contains non-finite number {name}"
    raise ProverToolError(msg)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a mapping, refusing a repeated object key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"snapshot manifest has duplicate key '{key}'"
            raise ProverToolError(msg)
        result[key] = value
    return result


def _structural_characters(text: str) -> cabc.Iterator[str]:
    """Yield characters outside JSON strings, honouring escapes."""
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        else:
            yield char


def _reject_excessive_nesting(text: str, field: str) -> None:
    """Reject container nesting deeper than the fixed ceiling.

    Parameters
    ----------
    text : str
        Decoded document text.
    field : str
        Document name used in the error message.

    Raises
    ------
    ProverToolError
        Raised when containers nest beyond `_MAX_NESTING_DEPTH`.
    """
    depth = 0
    for char in _structural_characters(text):
        if char in "[{":
            depth += 1
            if depth > _MAX_NESTING_DEPTH:
                msg = (
                    f"snapshot {field} is not valid JSON: "
                    f"nesting depth exceeds {_MAX_NESTING_DEPTH}"
                )
                raise ProverToolError(msg)
        elif char in "]}":
            depth -= 1


def strict_json_loads(data: bytes, field: str) -> object:
    """Decode strict UTF-8 JSON, rejecting duplicates and non-finite numbers.

    Parameters
    ----------
    data : bytes
        Exact document bytes.
    field : str
        Document name used in error messages.

    Returns
    -------
    object
        The decoded JSON value.

    Raises
    ------
    ProverToolError
        Raised for invalid UTF-8, syntax errors, duplicate keys, a non-finite
        number or nesting beyond `_MAX_NESTING_DEPTH`.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"snapshot {field} is not valid UTF-8: {exc}"
        raise ProverToolError(msg) from exc
    _reject_excessive_nesting(text, field)
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        msg = f"snapshot {field} is not valid JSON: {exc.msg}"
        raise ProverToolError(msg) from exc
    except (RecursionError, ValueError) as exc:
        # `json` also enforces an integer-string digit limit and a nesting
        # depth; both raise outside JSONDecodeError on hostile input.
        msg = f"snapshot {field} is not valid JSON: {exc}"
        raise ProverToolError(msg) from exc
