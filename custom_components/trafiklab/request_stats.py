"""Per-config-entry counters for real Trafiklab/Resrobot HTTP requests.

Counts are incremented from a callback the API client invokes right before
each outgoing HTTP call (see ``TrafikLabApiClient(on_request=...)``), so every
real request is counted exactly once regardless of which code path issued it
(scheduled coordinator refresh, ``trafiklab.update_now``, or platform
enrichment). Counts persist across HA restarts and options-triggered entry
reloads via a small per-entry storage file.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "trafiklab_request_stats"

# How many recent individual requests to remember for debugging bursts
# (e.g. distinguishing "one refresh fanned out into 3 chunks" from
# "3 separate refreshes happened within a minute").
RECENT_LOG_MAXLEN = 25


def _today_str() -> str:
    return datetime.now().date().isoformat()


class RequestStats:
    """Tracks and persists real HTTP requests made on behalf of one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}"
        )
        self.total: int = 0
        self.today: int = 0
        self._today_date: str = _today_str()
        self.last_request_at: str | None = None
        self.last_endpoint: str | None = None
        self.by_endpoint: dict[str, int] = {}
        self.recent: deque[dict] = deque(maxlen=RECENT_LOG_MAXLEN)

    async def async_load(self) -> None:
        """Load persisted counters, rolling over the daily counter if needed."""
        data = await self._store.async_load()
        if data:
            self.total = data.get("total", 0)
            self.today = data.get("today", 0)
            self._today_date = data.get("today_date", _today_str())
            self.last_request_at = data.get("last_request_at")
            self.last_endpoint = data.get("last_endpoint")
            self.by_endpoint = dict(data.get("by_endpoint") or {})
            for item in data.get("recent") or []:
                self.recent.append(item)
        self._roll_day()

    def _roll_day(self) -> None:
        today = _today_str()
        if self._today_date != today:
            self._today_date = today
            self.today = 0

    def record(self, endpoint: str) -> None:
        """Record one real outgoing HTTP request for ``endpoint``."""
        self._roll_day()
        self.total += 1
        self.today += 1
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + 1
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.last_request_at = now_iso
        self.last_endpoint = endpoint
        self.recent.append({"at": now_iso, "endpoint": endpoint})
        _LOGGER.debug(
            "Trafiklab request recorded (endpoint=%s, today=%d, total=%d)",
            endpoint,
            self.today,
            self.total,
        )
        self._store.async_delay_save(self._as_dict, 5)

    def _as_dict(self) -> dict:
        return {
            "total": self.total,
            "today": self.today,
            "today_date": self._today_date,
            "last_request_at": self.last_request_at,
            "last_endpoint": self.last_endpoint,
            "by_endpoint": self.by_endpoint,
            "recent": list(self.recent),
        }

    @property
    def today_value(self) -> int:
        """Return today's count, rolling over first if the day has changed."""
        self._roll_day()
        return self.today
