"""Data update coordinator for Trafiklab."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.template import Template
from homeassistant.exceptions import TemplateError

from .const import (
    CONF_API_KEY,
    CONF_STOP_ID,
    CONF_SENSOR_TYPE,
    CONF_REFRESH_INTERVAL,
    CONF_TIME_WINDOW,
    CONF_TRANSPORT_MODES,
    CONF_NIGHT_GAP_BY_MODE,
    CONF_MAX_TIMETABLE_CHUNKS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIME_WINDOW,
    DEFAULT_NIGHT_GAP_BY_MODE,
    DEFAULT_MAX_TIMETABLE_CHUNKS,
    MIN_MAX_TIMETABLE_CHUNKS,
    MAX_MAX_TIMETABLE_CHUNKS,
    TIMETABLE_API_WINDOW_MINUTES,
    TRANSPORT_MODES,
    SENSOR_TYPE_ARRIVAL,
    SENSOR_TYPE_DEPARTURE,
    SENSOR_TYPE_RESROBOT,
    CONF_UPDATE_CONDITION,
    RESROBOT_PRODUCTS_MAP,
    CONF_INCLUDE_PLATFORM,
    DOMAIN,
)
from .api import (
    TrafikLabApiClient,
    TrafikLabApiError,
    TrafikLabAuthError,
    TrafikLabQuotaError,
    TrafikLabServerError,
)
from .departure_limits import (
    departure_targets_from_config,
    departure_targets_satisfied,
    departure_targets_satisfied_by_direction,
    direction_targets_from_config,
    filters_from_config,
)
from .request_stats import RequestStats
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.issue_registry as ir

_LOGGER = logging.getLogger(__name__)


def _merge_timetable_chunks(responses: list[dict], list_key: str) -> dict:
    """Merge multiple Timetables API responses, deduplicating by trip + time."""
    merged_items: list[dict] = []
    seen: set[tuple] = set()
    for response in responses:
        for item in response.get(list_key) or []:
            trip_id = item.get("trip_id") or (item.get("trip") or {}).get(
                "trip_id"
            )
            key = (trip_id, item.get("scheduled"))
            if key in seen:
                continue
            seen.add(key)
            merged_items.append(item)
    merged_items.sort(
        key=lambda item: item.get("realtime") or item.get("scheduled") or ""
    )
    base = dict(responses[0])
    base[list_key] = merged_items
    return base


def _normalize_night_gap_by_mode(raw: Any) -> dict[str, tuple[int, int]]:
    """Return mode→(end_hour, start_hour) gaps from stored options."""
    if raw is None:
        raw = DEFAULT_NIGHT_GAP_BY_MODE
    if not isinstance(raw, dict):
        return {
            mode: (cfg["end"], cfg["start"])
            for mode, cfg in DEFAULT_NIGHT_GAP_BY_MODE.items()
        }

    gaps: dict[str, tuple[int, int]] = {}
    for mode, cfg in raw.items():
        if mode not in TRANSPORT_MODES or not isinstance(cfg, dict):
            continue
        try:
            end = int(cfg.get("end", 0))
            start = int(cfg.get("start", 0))
        except (TypeError, ValueError):
            continue
        if end > 0 and start > end:
            gaps[mode] = (end, start)
    return gaps


def _effective_night_gap(
    transport_modes: list[str] | None,
    by_mode: dict[str, tuple[int, int]],
) -> tuple[int, int] | None:
    """Return the combined dead zone, or None when gap skipping does not apply."""
    if transport_modes:
        scope = transport_modes
        gaps: list[tuple[int, int]] = []
        for mode in scope:
            gap = by_mode.get(mode)
            if gap is None:
                return None
            gaps.append(gap)
    else:
        gaps = list(by_mode.values())
        if not gaps:
            return None

    end_hour = max(end for end, _start in gaps)
    start_hour = min(start for _end, start in gaps)
    if end_hour >= start_hour:
        return None
    return end_hour, start_hour


def _in_night_service_gap(dt: datetime, night_gap: tuple[int, int]) -> bool:
    """Return True during the configured service gap."""
    end_hour, start_hour = night_gap
    return end_hour <= dt.hour < start_hour


def _night_gap_resume(dt: datetime, night_gap: tuple[int, int]) -> datetime:
    """Return the next service resume hour on the same calendar day as ``dt``."""
    _end_hour, start_hour = night_gap
    return dt.replace(hour=start_hour, minute=0, second=0, microsecond=0)


def _timetable_query_times(
    now: datetime,
    time_window_minutes: int,
    *,
    night_gap: tuple[int, int] | None,
) -> list[datetime | None]:
    """Build Timetables API query anchors for the configured time window."""
    window = max(1, min(int(time_window_minutes), 1440))
    desired_end = now + timedelta(minutes=window)

    if not night_gap or window <= TIMETABLE_API_WINDOW_MINUTES:
        query_times: list[datetime | None] = [None]
        offset = TIMETABLE_API_WINDOW_MINUTES
        while offset < window:
            query_times.append(now + timedelta(minutes=offset))
            offset += TIMETABLE_API_WINDOW_MINUTES
        return query_times

    queries: list[datetime | None] = []
    seen_anchors: set[datetime] = set()

    def add_query(anchor: datetime | None) -> None:
        key = now if anchor is None else anchor
        if key in seen_anchors:
            return
        seen_anchors.add(key)
        queries.append(anchor)

    if _in_night_service_gap(now, night_gap):
        resume = _night_gap_resume(now, night_gap)
        if resume > desired_end:
            return [None]
        add_query(resume)
        cursor = resume
    else:
        add_query(None)
        cursor = now

    while True:
        nxt = cursor + timedelta(minutes=TIMETABLE_API_WINDOW_MINUTES)
        if nxt > desired_end:
            break
        if _in_night_service_gap(nxt, night_gap):
            resume = _night_gap_resume(nxt, night_gap)
            if resume > desired_end:
                break
            add_query(resume)
            cursor = resume
            continue
        add_query(nxt)
        cursor = nxt

    return queries


def _coerce_max_timetable_chunks(value: Any) -> int:
    """Return a clamped max chunk count for Timetables API fetches."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_TIMETABLE_CHUNKS
    return max(MIN_MAX_TIMETABLE_CHUNKS, min(MAX_MAX_TIMETABLE_CHUNKS, parsed))


class TrafikLabCoordinator(DataUpdateCoordinator):
    """Data update coordinator for Trafiklab."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        # Use HA shared aiohttp session
        session = async_get_clientsession(hass)
        # Persisted per-entry counter of real outgoing HTTP requests — survives
        # restarts and options-triggered reloads. Loaded async in __init__.py
        # before the first refresh; see async_setup_entry.
        self.request_stats = RequestStats(hass, entry.entry_id)
        self.api_client = TrafikLabApiClient(
            entry.data[CONF_API_KEY],
            session=session,
            on_request=self.request_stats.record,
        )
        # Track last successful update (UTC ISO8601)
        self.last_successful_update: str | None = None
        # Track last API error (None when healthy)
        self.last_api_error: dict | None = None

        # Options override data if present
        refresh_interval = entry.options.get(
            CONF_REFRESH_INTERVAL,
            entry.data.get(CONF_REFRESH_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        super().__init__(
            hass,
            _LOGGER,
            name="Trafiklab",
            update_interval=timedelta(seconds=refresh_interval),
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from Trafiklab API."""
        try:
            # Optional: evaluate update condition template from options
            update_condition_tmpl = (self.entry.options.get(CONF_UPDATE_CONDITION) or "").strip()
            if update_condition_tmpl:
                try:
                    rendered = Template(update_condition_tmpl, self.hass).async_render(None)
                    if isinstance(rendered, str):
                        cond = rendered.strip().lower() == "true"
                    else:
                        cond = bool(rendered)
                    if not cond:
                        _LOGGER.debug("Update condition evaluated to false; skipping API call")
                        # Return the last known data (do not mark update as failed)
                        return self.data or {}
                except TemplateError as terr:
                    _LOGGER.warning("Update condition template error: %s", terr)
                    # Proceed with update to avoid stalling sensor
            # Merge data + options (options override)
            # Base immutable data lives in entry.data (api key, stop id, sensor_type, name)
            # Mutable settings now live in options (migration safe fallback to data)
            sensor_type = self.entry.data.get(CONF_SENSOR_TYPE, SENSOR_TYPE_DEPARTURE)
            if sensor_type == SENSOR_TYPE_RESROBOT:
                # Gather Resrobot parameters
                base = self.entry.data
                opts = self.entry.options
                api_key = base[CONF_API_KEY]
                origin_type = base.get("origin_type", "stop_id")
                origin = base.get("origin", "")
                destination_type = base.get("destination_type", "stop_id")
                destination = base.get("destination", "")
                via = opts.get("via", "")
                avoid = opts.get("avoid", "")
                max_walking_distance = opts.get("max_walking_distance", 1000)
                # Build per-mode products bitmasks so each mode gets a fair set of
                # results.  When only a single mode (or no filter) is selected we
                # keep the existing single-call behaviour.  When multiple modes are
                # selected we issue one parallel request per mode and merge the
                # trips afterwards – this prevents the route planner from filling
                # all result slots with the fastest mode only.
                transport_modes = opts.get(CONF_TRANSPORT_MODES) or []
                known_modes = [m for m in transport_modes if m in RESROBOT_PRODUCTS_MAP]

                async def _fetch_for_products(products_bitmask: int | None) -> dict:
                    return await self.api_client.get_resrobot_travel_search(
                        api_key,
                        origin_type,
                        origin,
                        destination_type,
                        destination,
                        via,
                        avoid,
                        max_walking_distance,
                        products_bitmask,
                    )

                if len(known_modes) > 1:
                    # One request per mode – run concurrently
                    _LOGGER.debug(
                        "Fetching Resrobot travel search with %d separate mode calls: %s → %s",
                        len(known_modes), origin, destination,
                    )
                    per_mode_results = await asyncio.gather(
                        *[_fetch_for_products(RESROBOT_PRODUCTS_MAP[m]) for m in known_modes]
                    )
                    # Merge Trip lists from all responses; keep the first response's
                    # metadata envelope (Trip is the only field we care about).
                    merged_trips: list = []
                    seen_trip_keys: set = set()
                    for result in per_mode_results:
                        for trip in (result or {}).get("Trip") or []:
                            # Deduplicate by (first-leg origin/dest names + departure time)
                            legs = (((trip or {}).get("LegList") or {}).get("Leg")) or []
                            if isinstance(legs, dict):
                                legs = [legs]
                            first_leg = legs[0] if legs else {}
                            last_leg = legs[-1] if legs else {}
                            trip_key = (
                                (first_leg.get("Origin") or {}).get("name", ""),
                                (first_leg.get("Origin") or {}).get("date", ""),
                                (first_leg.get("Origin") or {}).get("time", ""),
                                (last_leg.get("Destination") or {}).get("name", ""),
                                (last_leg.get("Destination") or {}).get("time", ""),
                            )
                            if trip_key not in seen_trip_keys:
                                seen_trip_keys.add(trip_key)
                                merged_trips.append(trip)
                    data = dict(per_mode_results[0]) if per_mode_results else {}
                    data["Trip"] = merged_trips
                else:
                    # Single mode or no filter – original single-call path
                    products: int | None = RESROBOT_PRODUCTS_MAP.get(known_modes[0]) if known_modes else None
                    _LOGGER.debug(
                        "Fetching Resrobot travel search: origin=%s, destination=%s", origin, destination,
                    )
                    data = await _fetch_for_products(products)

                # Normalize and sort trips/legs for consistent downstream usage
                try:
                    data = self._normalize_resrobot_response(data)
                except Exception as nerr:  # pragma: no cover - defensive
                    _LOGGER.debug("Resrobot normalize failed: %s", nerr)

                # Platform enrichment — opt-in via include_platform option
                if opts.get(CONF_INCLUDE_PLATFORM):
                    try:
                        data = await self._enrich_platform(data)
                    except Exception as perr:
                        _LOGGER.warning("Platform enrichment failed: %s", perr)
                # Mark successful update time
                from datetime import datetime, timezone
                self.last_successful_update = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                self.last_api_error = None
                ir.async_delete_issue(self.hass, DOMAIN, f"invalid_api_key_{self.entry.entry_id}")
                return data
            else:
                stop_id = self.entry.data[CONF_STOP_ID]
                merged_opts = {**self.entry.data, **self.entry.options}
                time_window = int(
                    merged_opts.get(CONF_TIME_WINDOW, DEFAULT_TIME_WINDOW)
                )
                _LOGGER.debug(
                    "Fetching %s for stop %s (time_window=%d min)",
                    sensor_type,
                    stop_id,
                    time_window,
                )
                data = await self._fetch_timetable(
                    stop_id,
                    arrivals=(sensor_type == SENSOR_TYPE_ARRIVAL),
                    time_window_minutes=time_window,
                )
                if not data:
                    _LOGGER.warning("No data received from API")
                    raise UpdateFailed("No data received from API")
                if not isinstance(data, dict):
                    _LOGGER.error("Invalid API response format: expected dict, got %s", type(data))
                    raise UpdateFailed("Invalid API response format")
                _LOGGER.debug("API response keys: %s", list(data.keys()))
                # Check if we have the expected data structure at the top level
                if sensor_type == SENSOR_TYPE_ARRIVAL and "arrivals" in data:
                    _LOGGER.debug("Found %d arrivals", len(data["arrivals"]))
                elif sensor_type == SENSOR_TYPE_DEPARTURE and "departures" in data:
                    _LOGGER.debug("Found %d departures", len(data["departures"]))
                else:
                    _LOGGER.warning("No departure/arrival data at top level: %s", list(data.keys()))
                # Mark successful update time (UTC ISO8601 without microseconds)
                from datetime import datetime, timezone
                self.last_successful_update = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                self.last_api_error = None
                ir.async_delete_issue(self.hass, DOMAIN, f"invalid_api_key_{self.entry.entry_id}")
                return data
            
        except TrafikLabAuthError as err:
            self.last_api_error = {
                "code": err.error_code or "auth_error",
                "message": str(err),
                "http_status": err.http_status,
            }
            _LOGGER.error(
                "Authentication error for %s — API key is invalid or unauthorized: %s",
                self.entry.title,
                err,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"invalid_api_key_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="invalid_api_key",
                translation_placeholders={"entry_title": self.entry.title},
            )
            raise UpdateFailed(f"Authentication error: {err}") from err
        except TrafikLabQuotaError as err:
            self.last_api_error = {
                "code": err.error_code or "quota_exceeded",
                "message": str(err),
                "http_status": err.http_status,
            }
            _LOGGER.warning(
                "Quota or rate-limit exceeded for %s: %s", self.entry.title, err
            )
            raise UpdateFailed(f"Quota exceeded: {err}") from err
        except TrafikLabServerError as err:
            self.last_api_error = {
                "code": err.error_code or "server_error",
                "message": str(err),
                "http_status": err.http_status,
            }
            _LOGGER.warning(
                "Trafiklab server error for %s: %s", self.entry.title, err
            )
            raise UpdateFailed(f"Server error: {err}") from err
        except TrafikLabApiError as err:
            self.last_api_error = {
                "code": err.error_code or "api_error",
                "message": str(err),
                "http_status": err.http_status,
            }
            _LOGGER.error("Error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            self.last_api_error = {
                "code": "unexpected_error",
                "message": str(err),
                "http_status": None,
            }
            _LOGGER.error("Unexpected error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _fetch_timetable(
        self,
        stop_id: str,
        *,
        arrivals: bool,
        time_window_minutes: int,
    ) -> dict:
        """Fetch departures/arrivals for the configured time window.

        The Trafiklab Timetables API always returns 60 minutes per request.
        When ``time_window_minutes`` exceeds that, additional requests are
        made at +60, +120, … minute offsets and merged.

        Fetching stops early when:
        - configured departure limits are already filled, or
        - a chunk returns no items, or
        - ``max_timetable_chunks`` is reached.
        """
        fetch_fn = (
            self.api_client.get_arrivals if arrivals else self.api_client.get_departures
        )
        list_key = "arrivals" if arrivals else "departures"
        time_window = max(1, min(int(time_window_minutes), 1440))
        merged_opts = {**self.entry.data, **self.entry.options}
        transport_modes = merged_opts.get(CONF_TRANSPORT_MODES) or []
        night_gap = _effective_night_gap(
            transport_modes or None,
            _normalize_night_gap_by_mode(merged_opts.get(CONF_NIGHT_GAP_BY_MODE)),
        )
        max_chunks = _coerce_max_timetable_chunks(
            merged_opts.get(CONF_MAX_TIMETABLE_CHUNKS, DEFAULT_MAX_TIMETABLE_CHUNKS)
        )
        default_max, by_mode = departure_targets_from_config(merged_opts)
        _, by_direction = direction_targets_from_config(merged_opts)
        filter_kwargs = filters_from_config(merged_opts)

        now = datetime.now().replace(second=0, microsecond=0)
        query_times = _timetable_query_times(
            now,
            time_window,
            night_gap=night_gap,
        )[:max_chunks]

        responses: list[dict] = []
        for query_time in query_times:
            if query_time is None:
                chunk = await fetch_fn(stop_id)
            else:
                chunk = await fetch_fn(
                    stop_id, query_time.strftime("%Y-%m-%dT%H:%M")
                )
            chunk_items = (chunk or {}).get(list_key) or []
            if not chunk or not chunk_items:
                _LOGGER.debug(
                    "Timetable chunk empty at %s; stopping further fetches",
                    query_time or "now",
                )
                if chunk:
                    responses.append(chunk)
                break

            responses.append(chunk)
            merged_so_far = _merge_timetable_chunks(responses, list_key)
            merged_items = merged_so_far.get(list_key) or []
            # Per-direction targets (when configured) take priority over
            # per-mode ones — they're mutually exclusive concerns for a given
            # entry: either you're capping by transport mode, or by direction.
            targets_satisfied = (
                departure_targets_satisfied_by_direction(
                    merged_items, default_max, by_direction, **filter_kwargs
                )
                if by_direction
                else departure_targets_satisfied(
                    merged_items, default_max, by_mode, **filter_kwargs
                )
            )
            if targets_satisfied:
                _LOGGER.debug(
                    "Departure limits filled after %d chunk(s); stopping further fetches",
                    len(responses),
                )
                break

        if not responses:
            return {}

        merged = _merge_timetable_chunks(responses, list_key)
        _LOGGER.debug(
            "Fetched %d timetable chunk(s)%s; merged to %d %s",
            len(responses),
            f" (night gap {night_gap[0]:02d}:00–{night_gap[1]:02d}:00)" if night_gap else "",
            len(merged.get(list_key) or []),
            list_key,
        )
        return merged

    def _normalize_resrobot_response(self, data: dict) -> dict:
        """Ensure ResRobot response has sorted trips and legs.

        - Guarantees data["Trip"] is a list.
        - Ensures each trip's LegList.Leg is a list.
        - Sorts trips by first leg origin datetime (ascending).
        - Sorts legs within each trip by origin datetime (ascending).
        """
        from datetime import datetime

        if not isinstance(data, dict):
            return data

        trips = data.get("Trip")
        if trips is None:
            data["Trip"] = []
            return data
        if isinstance(trips, dict):
            trips = [trips]

        def parse_dt(leg: dict) -> tuple:
            org = leg.get("Origin", {}) or {}
            d = org.get("date") or org.get("rtDate") or "9999-12-31"
            t = org.get("time") or org.get("rtTime") or "23:59:59"
            # Try with seconds, fallback without
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return (datetime.strptime(f"{d} {t}", fmt),)
                except Exception:
                    continue
            # Unparsable -> push to end, include secondary sort key to keep stability
            return (datetime.max,)

        normalized_trips: list[dict] = []
        for trip in trips:
            legs = (((trip or {}).get("LegList") or {}).get("Leg"))
            if legs is None:
                legs_list: list[dict] = []
            elif isinstance(legs, dict):
                legs_list = [legs]
            else:
                legs_list = list(legs)
            # Sort legs by origin datetime
            legs_list.sort(key=lambda lg: (parse_dt(lg)[0], lg.get("idx", 0)))
            # Write back sorted list
            trip = dict(trip or {})
            leglist = dict((trip.get("LegList") or {}))
            leglist["Leg"] = legs_list
            trip["LegList"] = leglist
            normalized_trips.append(trip)

        # Sort trips by their first leg datetime or trip-level Origin
        def trip_key(tp: dict):
            legs_list = (((tp or {}).get("LegList") or {}).get("Leg")) or []
            if legs_list:
                return parse_dt(legs_list[0])[0]
            # fallback to trip-level Origin
            org = (tp or {}).get("Origin", {}) or {}
            d = org.get("date") or "9999-12-31"
            t = org.get("time") or "23:59:59"
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(f"{d} {t}", fmt)
                except Exception:
                    continue
            return datetime.max

        normalized_trips.sort(key=trip_key)
        data["Trip"] = normalized_trips
        return data

    async def _enrich_platform(self, data: dict) -> dict:
        """Annotate each public-transport leg in *data* with ``_realtime_platform``.

        Resolves a Realtime API key from any departure/arrival entry in hass.data,
        then issues one Timetable API call per unique leg-origin stop ID (batched
        concurrently). Each matching leg gets ``_realtime_platform`` set to the
        platform designation string (empty string when no match found).
        """
        # Resolve Realtime API key from a departure/arrival sensor entry
        realtime_key: str | None = None
        domain_data: dict = self.hass.data.get(DOMAIN, {})
        for coordinator in domain_data.values():
            entry_type = (coordinator.entry.data.get(CONF_SENSOR_TYPE)
                          if hasattr(coordinator, "entry") else None)
            if entry_type in (SENSOR_TYPE_DEPARTURE, SENSOR_TYPE_ARRIVAL):
                realtime_key = coordinator.entry.data.get(CONF_API_KEY)
                break

        if not realtime_key:
            _LOGGER.warning(
                "include_platform is enabled but no departure/arrival sensor with a "
                "Realtime API key was found. Platform information will not be included."
            )
            return data

        trips: list = (data or {}).get("Trip") or []
        await enrich_platform_for_trips(
            trips, realtime_key, self.api_client.session, on_request=self.request_stats.record
        )
        return data


# ---------------------------------------------------------------------------
# Module-level helper — shared by coordinator and services_setup
# ---------------------------------------------------------------------------

_NON_PT_TYPES: frozenset[str] = frozenset({"WALK", "TRSF"})

# Maximum number of concurrent Timetable API calls when enriching platforms.
# Prevents bursting too many requests at once when a trip response contains
# many unique origin stops. 5 is a conservative ceiling that keeps the
# request burst well within typical Trafiklab rate-limit budgets while still
# providing useful parallelism for the common case (1-3 unique stops per trip).
_PLATFORM_ENRICH_CONCURRENCY: int = 5


async def enrich_platform_for_trips(
    trips: list,
    realtime_api_key: str,
    session,
    *,
    on_request=None,
) -> None:
    """Annotate public-transport legs in *trips* with ``_realtime_platform``.

    Modifies the raw Resrobot trip dicts in-place. Each public-transport leg
    (type not in WALK/TRSF) whose ``Origin.extId`` is non-empty gets a
    ``_realtime_platform`` key set to the platform designation string from the
    Timetable Realtime API (empty string when no match is found).

    Issues one Timetable API call per unique origin stop ID, batched concurrently,
    each covering a 60-minute window starting from the earliest departure at that stop.
    """
    from datetime import datetime

    # ------------------------------------------------------------------
    # 1. Collect unique stop IDs with earliest departure datetime
    # ------------------------------------------------------------------
    stop_earliest: dict[str, datetime] = {}

    for trip in trips:
        legs = (((trip or {}).get("LegList") or {}).get("Leg")) or []
        if isinstance(legs, dict):
            legs = [legs]
        for leg in legs:
            if str((leg or {}).get("type", "")).upper() in _NON_PT_TYPES:
                continue
            origin = (leg or {}).get("Origin") or {}
            ext_id = origin.get("extId", "").strip()
            if not ext_id:
                continue
            date_str = origin.get("date", "")
            time_str = origin.get("time", "")
            if not date_str or not time_str:
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dep_dt = datetime.strptime(f"{date_str} {time_str[:8]}", fmt)
                    break
                except ValueError:
                    dep_dt = None
            if dep_dt is None:
                continue
            if ext_id not in stop_earliest or dep_dt < stop_earliest[ext_id]:
                stop_earliest[ext_id] = dep_dt

    if not stop_earliest:
        return

    # ------------------------------------------------------------------
    # 2. Fetch Timetable departures for each unique stop concurrently
    #    (concurrency capped by semaphore to avoid request bursts)
    # ------------------------------------------------------------------
    client = TrafikLabApiClient(realtime_api_key, session=session, on_request=on_request)
    semaphore = asyncio.Semaphore(_PLATFORM_ENRICH_CONCURRENCY)

    async def _fetch(stop_id: str, earliest: datetime):
        async with semaphore:
            time_str = earliest.strftime("%Y-%m-%dT%H:%M")
            try:
                result = await client.get_departures(stop_id, time_str)
                return stop_id, result
            except Exception as err:
                _LOGGER.debug("Timetable call failed for stop %s: %s", stop_id, err)
                return stop_id, {}

    results = await asyncio.gather(
        *[_fetch(sid, dt) for sid, dt in stop_earliest.items()]
    )

    # ------------------------------------------------------------------
    # 3. Build per-stop lookup: {stop_id: {(designation, "HH:MM"): platform}}
    # ------------------------------------------------------------------
    stop_lookup: dict[str, dict[tuple[str, str], str]] = {}
    for stop_id, result in results:
        departures: list = (result or {}).get("departures") or []
        lookup: dict[tuple[str, str], str] = {}
        for dep in departures:
            route = (dep or {}).get("route") or {}
            designation = str(route.get("designation", "")).strip()
            scheduled = dep.get("scheduled", "")
            # scheduled is ISO8601; extract HH:MM
            try:
                hhmm = scheduled[11:16] if len(scheduled) >= 16 else ""
            except Exception:
                hhmm = ""
            if not designation or not hhmm:
                continue
            platform_str = (
                (dep.get("realtime_platform") or {}).get("designation")
                or (dep.get("scheduled_platform") or {}).get("designation")
                or ""
            )
            key = (designation, hhmm)
            if key not in lookup:  # keep first match (ambiguity is rare)
                lookup[key] = platform_str
        stop_lookup[stop_id] = lookup

    # ------------------------------------------------------------------
    # 4. Annotate legs in the raw trip data
    # ------------------------------------------------------------------
    for trip in trips:
        legs = (((trip or {}).get("LegList") or {}).get("Leg")) or []
        if isinstance(legs, dict):
            legs = [legs]
        for leg in legs:
            if str((leg or {}).get("type", "")).upper() in _NON_PT_TYPES:
                continue
            origin = (leg or {}).get("Origin") or {}
            ext_id = origin.get("extId", "").strip()
            if not ext_id or ext_id not in stop_lookup:
                continue
            # Match designation: prefer leg.number, fall back to product.displayNumber
            product = (leg or {}).get("Product") or {}
            if isinstance(product, list):
                product = product[0] if product else {}
            designation = str(
                leg.get("number") or product.get("displayNumber") or product.get("num") or ""
            ).strip()
            time_str = origin.get("time", "")
            hhmm = time_str[:5] if len(time_str) >= 5 else ""
            platform = stop_lookup[ext_id].get((designation, hhmm), "")
            leg["_realtime_platform"] = platform
