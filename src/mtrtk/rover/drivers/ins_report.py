"""Vendor-neutral JSON views of an INS configuration report and unit identity.

`SbgConfigReport` and `VnConfigReport` carry the same idea (what was read, applied, left pending
or read back wrong) with slightly different fields. The API, the WebSocket and `mtrtk ins`
show them through one shape, so the UI renders either vendor without knowing which:

- `items`: one row per profile item, `{name, state, current, wanted}`, where `state` is one of
  `ITEM_STATES` (`error` for an item named in an error message, which has no list of its own);
- the vendor's own lists (`applied`, `unchanged`, `pending`, `mismatched`, `unsupported`,
  `errors`), `notes` (empty for SBG), `saved`, and the raw `current` / `wanted` values.

Imports nothing vendor-specific: the web layer reads it without loading either driver.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from enum import Enum
from typing import Any

ITEM_STATES = ("mismatched", "pending", "applied", "unchanged", "unsupported")


def jsonable(value: Any) -> Any:
    """Dataclasses, dates, enums, tuples and bytes as JSON-ready values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def _names(report: Any, attr: str) -> list[str]:
    value = getattr(report, attr, None)
    return [str(v) for v in value] if isinstance(value, list) else []


def report_dict(report: Any) -> dict[str, Any] | None:
    """The common JSON shape of a vendor configuration report (see the module docstring)."""
    if report is None:
        return None
    lists = {name: _names(report, name) for name in (*ITEM_STATES, "errors", "notes")}
    current = getattr(report, "current", None) or {}
    wanted = getattr(report, "wanted", None) or {}
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for state in ITEM_STATES:
        for name in lists[state]:
            if name in seen:
                continue
            seen.add(name)
            items.append(
                {
                    "name": name,
                    "state": state,
                    "current": jsonable(current.get(name)),
                    "wanted": jsonable(wanted.get(name)),
                }
            )
    # An item that failed is named at the start of its error message ("motion_profile: ...").
    for message in lists["errors"]:
        name = message.split(":", 1)[0].strip()
        if name and name not in seen and " " not in name:
            seen.add(name)
            items.append(
                {"name": name, "state": "error", "current": None, "wanted": None, "error": message}
            )
    return {
        "items": items,
        **lists,
        "saved": bool(getattr(report, "saved", False)),
        "current": jsonable(current),
        "wanted": jsonable(wanted),
    }


def needs_apply(report: dict[str, Any] | None) -> bool:
    """True when applying the profile would change something on the unit."""
    return bool(report and (report.get("pending") or report.get("mismatched")))
