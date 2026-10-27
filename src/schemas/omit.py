"""Helper for response fields that only exist while the multi-field workflow is on.

A new optional field on an existing response model must not appear as `null` in the
JSON while `fieldWorkflowEnabled` is off, or the response would change for every
client. Models list their multi-field keys and drop any that are None when
serialised; the OpenAPI schema still describes them.
"""
from __future__ import annotations

from typing import Any


def dropNoneKeys(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """`data` without those of `keys` whose value is None (other keys, even None ones, stay)."""
    for key in keys:
        if data.get(key) is None:
            data.pop(key, None)
    return data
