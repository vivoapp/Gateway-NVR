"""RFC 3339 UTC times at the API boundary; Unix seconds inside (contract section 2)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .errors import ApiError

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]00:00)$")


def parse(value: str, name: str = "time") -> float:
    """'2026-11-08T10:15:00Z' -> Unix seconds. Only UTC is accepted."""
    if not isinstance(value, str) or not _RFC3339.match(value):
        raise ApiError(400, "invalid_argument", f"{name} must be an RFC 3339 UTC time ending in Z.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "invalid_argument", f"{name} is not a valid time.") from None
    return dt.timestamp()


def fmt(t: float) -> str:
    """Unix seconds -> '2026-11-08T10:15:00Z', with milliseconds only when there are any."""
    dt = datetime.fromtimestamp(round(t, 3), tz=timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(t: float) -> str:
    """Unix seconds -> '20261108T094000Z' (file names)."""
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def window(start: str, end: str, max_seconds: int) -> tuple[float, float]:
    s, e = parse(start, "start"), parse(end, "end")
    if e <= s:
        raise ApiError(400, "invalid_argument", "end must be after start.")
    if e - s > max_seconds:
        raise ApiError(413, "range_too_long", f"The range is longer than {max_seconds} s.")
    return s, e
