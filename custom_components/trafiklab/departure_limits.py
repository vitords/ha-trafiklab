"""Shared departure/arrival limit and filter helpers."""
from __future__ import annotations

from typing import Any

from .const import (
    API_TRANSPORT_MODE_TO_KEY,
    CONF_DIRECTION,
    CONF_LINE_FILTER,
    CONF_MAX_DEPARTURES,
    CONF_MAX_DEPARTURES_BY_DIRECTION,
    CONF_MAX_DEPARTURES_BY_MODE,
    CONF_TRANSPORT_MODES,
    DEFAULT_MAX_DEPARTURES,
    MAX_MAX_DEPARTURES,
    MIN_MAX_DEPARTURES,
    TRANSPORT_MODES,
)


def coerce_max_departures(value: Any) -> int:
    """Return a clamped max departures count from config data."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_DEPARTURES
    return max(MIN_MAX_DEPARTURES, min(MAX_MAX_DEPARTURES, parsed))


def normalize_max_departures_by_mode(raw: Any) -> dict[str, int]:
    """Return a cleaned mode→limit map from options storage."""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for mode, value in raw.items():
        if mode not in TRANSPORT_MODES or value is None:
            continue
        cleaned[mode] = coerce_max_departures(value)
    return cleaned


def item_mode_key(item: dict) -> str:
    """Return the config-key transport mode for a timetable item."""
    api_mode = ((item.get("route") or {}).get("transport_mode") or "").upper()
    return API_TRANSPORT_MODE_TO_KEY.get(api_mode, api_mode.lower())


def limit_items_by_mode(
    items: list[dict],
    default_max: int,
    by_mode: dict[str, int] | None,
) -> list[dict]:
    """Limit departures/arrivals, optionally with per-transport-mode caps.

    When ``by_mode`` is empty, keeps the legacy behaviour of a single overall
    cap (``items[:default_max]``).

    When any per-mode limit is set, each mode is capped independently (falling
    back to ``default_max`` for modes without an override). Chronological order
    of the input list is preserved.
    """
    limits = normalize_max_departures_by_mode(by_mode)
    if not limits:
        return items[:default_max]

    counts: dict[str, int] = {}
    limited: list[dict] = []
    for item in items:
        mode_key = item_mode_key(item)
        limit = limits.get(mode_key, default_max)
        taken = counts.get(mode_key, 0)
        if taken >= limit:
            continue
        counts[mode_key] = taken + 1
        limited.append(item)
    return limited


def parse_max_departures_by_direction(raw: Any) -> dict[str, int]:
    """Parse a 'token:count, token:count' string into a direction→limit map.

    Tokens are matched later as case-insensitive substrings against each
    item's destination (same rule as the Destination Filter). Malformed or
    empty tokens are skipped rather than raising, since this comes straight
    from a free-text config field.
    """
    result: dict[str, int] = {}
    if not raw:
        return result
    for part in str(raw).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        token, _, count_str = part.partition(":")
        token = token.strip()
        if not token:
            continue
        result[token] = coerce_max_departures(count_str.strip())
    return result


def item_direction_bucket(item: dict, tokens: list[str]) -> str | None:
    """Return the first configured direction token whose substring matches this item."""
    destination = ((item.get("route") or {}).get("direction") or "").lower()
    for token in tokens:
        if token.lower() in destination:
            return token
    return None


def limit_items_by_direction(
    items: list[dict],
    default_max: int,
    by_direction: dict[str, int] | None,
) -> list[dict]:
    """Limit departures/arrivals with independent per-direction caps.

    Mirrors ``limit_items_by_mode`` but buckets by destination substring
    instead of transport mode, so e.g. 'söder' and 'norr' each keep their own
    quota regardless of how the other direction is served. Items matching no
    configured token fall back to the shared ``default_max`` bucket.
    """
    if not by_direction:
        return items[:default_max]

    tokens = list(by_direction.keys())
    counts: dict[str, int] = {}
    limited: list[dict] = []
    for item in items:
        bucket = item_direction_bucket(item, tokens)
        limit = by_direction[bucket] if bucket else default_max
        key = bucket or "\0unmatched"
        taken = counts.get(key, 0)
        if taken >= limit:
            continue
        counts[key] = taken + 1
        limited.append(item)
    return limited


def direction_targets_from_config(merged_cfg: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Return (default_max, per_direction_limits) from entry data/options."""
    default_max = coerce_max_departures(
        merged_cfg.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES)
    )
    by_direction = parse_max_departures_by_direction(
        merged_cfg.get(CONF_MAX_DEPARTURES_BY_DIRECTION)
    )
    return default_max, by_direction


def departure_targets_satisfied_by_direction(
    items: list[dict],
    default_max: int,
    by_direction: dict[str, int] | None,
    *,
    line_filter: str = "",
    direction: str = "",
    transport_modes: list[str] | None = None,
) -> bool:
    """Return True once every configured direction bucket has reached its cap."""
    filtered = filter_timetable_items(
        items,
        line_filter=line_filter,
        direction=direction,
        transport_modes=transport_modes,
    )
    if not by_direction:
        return len(filtered) >= default_max

    tokens = list(by_direction.keys())
    counts: dict[str, int] = {}
    for item in filtered:
        bucket = item_direction_bucket(item, tokens)
        if bucket:
            counts[bucket] = counts.get(bucket, 0) + 1

    return all(counts.get(token, 0) >= limit for token, limit in by_direction.items())


def filter_timetable_items(
    items: list[dict],
    *,
    line_filter: str = "",
    direction: str = "",
    transport_modes: list[str] | None = None,
) -> list[dict]:
    """Apply line, direction, and transport-mode filters to timetable items."""
    line_filter = (line_filter or "").strip()
    direction_tokens = [
        t.strip().lower() for t in (direction or "").split(",") if t.strip()
    ]
    line_set = (
        {ln.strip() for ln in line_filter.split(",") if ln.strip()}
        if line_filter
        else None
    )
    transport_mode_filter = {
        TRANSPORT_MODES.get(m, m).upper()
        for m in (transport_modes or [])
        if m
    }

    def match_line(item: dict) -> bool:
        if not line_set:
            return True
        designation = item.get("route", {}).get("designation", "")
        return designation in line_set

    def match_direction(item: dict) -> bool:
        if not direction_tokens:
            return True
        destination = item.get("route", {}).get("direction", "")
        dest_lower = destination.lower()
        return any(tok in dest_lower for tok in direction_tokens)

    def match_transport_mode(item: dict) -> bool:
        if not transport_mode_filter:
            return True
        mode = (item.get("route") or {}).get("transport_mode", "").upper()
        return mode in transport_mode_filter

    return [
        it
        for it in items
        if match_line(it) and match_direction(it) and match_transport_mode(it)
    ]


def departure_targets_from_config(merged_cfg: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Return (default_max, per_mode_limits) from entry data/options."""
    default_max = coerce_max_departures(
        merged_cfg.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES)
    )
    by_mode = normalize_max_departures_by_mode(
        merged_cfg.get(CONF_MAX_DEPARTURES_BY_MODE)
    )
    return default_max, by_mode


def departure_targets_satisfied(
    items: list[dict],
    default_max: int,
    by_mode: dict[str, int] | None,
    *,
    line_filter: str = "",
    direction: str = "",
    transport_modes: list[str] | None = None,
) -> bool:
    """Return True when filtered items already fill the configured limits.

    With per-mode limits: every mode in ``by_mode`` (intersected with any
    transport-mode filter) must reach its cap.

    Without per-mode limits: overall filtered count must reach ``default_max``.
    """
    filtered = filter_timetable_items(
        items,
        line_filter=line_filter,
        direction=direction,
        transport_modes=transport_modes,
    )
    limits = normalize_max_departures_by_mode(by_mode)
    if not limits:
        return len(filtered) >= default_max

    counts: dict[str, int] = {}
    for item in filtered:
        mode_key = item_mode_key(item)
        counts[mode_key] = counts.get(mode_key, 0) + 1

    required_modes = set(limits)
    if transport_modes:
        required_modes &= set(transport_modes)
    if not required_modes:
        return len(filtered) >= default_max

    return all(counts.get(mode, 0) >= limits[mode] for mode in required_modes)


def filters_from_config(merged_cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract filter kwargs from merged entry data/options."""
    return {
        "line_filter": (merged_cfg.get(CONF_LINE_FILTER) or ""),
        "direction": (merged_cfg.get(CONF_DIRECTION) or ""),
        "transport_modes": merged_cfg.get(CONF_TRANSPORT_MODES) or [],
    }
