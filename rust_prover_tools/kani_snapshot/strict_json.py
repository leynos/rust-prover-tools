"""Strict JSON decoding for untrusted snapshot documents.

Python's default decoder accepts duplicate object keys (last one wins) and the
non-finite extensions `NaN`/`Infinity`. A manifest whose parsed meaning depends
on those leniencies could pass review in one form and verify in another, so
both are refused here.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = ("strict_json_loads",)


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
        Raised for invalid UTF-8, syntax errors, duplicate keys or a
        non-finite number.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"snapshot {field} is not valid UTF-8: {exc}"
        raise ProverToolError(msg) from exc
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
