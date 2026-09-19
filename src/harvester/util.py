"""Small shared helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    """Current UTC time as a stable ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_json(value: Any) -> str:
    """Deterministic JSON serialisation.

    ``sort_keys`` keeps sidecars and reports byte-identical for identical inputs,
    which is what MASTER_SPEC section 3.5 (determinism) requires of the output.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def to_pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def parse_json(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion that never fabricates a value."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
