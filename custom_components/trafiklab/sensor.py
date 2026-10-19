"""Sensor platform for Trafiklab integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    CONF_SENSOR_TYPE,
    CONF_DIRECTION,
    CONF_LINE_FILTER,
    CONF_MAX_TRIP_DURATION,
    CONF_TRANSPORT_MODES,
    DEFAULT_TIME_WINDOW,
    CONF_TIME_WINDOW,
    SENSOR_TYPE_ARRIVAL,
    SENSOR_TYPE_RESROBOT,
)
from .coordinator import TrafikLabCoordinator
from .departure_limits import (
    departure_targets_from_config,
    direction_targets_from_config,
    filter_timetable_items,
    limit_items_by_direction as _limit_items_by_direction,
    limit_items_by_mode as _limit_items_by_mode,
)

_LOGGER = logging.getLogger(__name__)


def _notices_from_alerts(item: dict[str, Any]) -> list[str]:
    """Extract human-readable notices from a departure/arrival alerts array.

    The Realtime Timetables API exposes ``alerts`` as a list of
    ``{type, title, text}`` objects. Empty / missing alerts yield ``[]``.
    """
    raw = item.get("alerts")
    if not raw:
        return []
    if isinstance(raw, dict):
        alerts = [raw]
    elif isinstance(raw, list):
        alerts = raw
    else:
        return [str(raw)]

    notices: list[str] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            if alert:
                notices.append(str(alert))
            continue
        title = (alert.get("title") or "").strip()
        text = (alert.get("text") or "").strip()
        if title and text and text != title:
            notices.append(f"{title}: {text}")
        elif title or text:
            notices.append(title or text)
    return notices


def _slugify(value: str) -> str:
    """Simple slugify helper for entity_id parts: lowercase, underscores, safe chars only."""
    import re
    v = (value or "").strip().lower()
    v = re.sub(r"\s+", "_", v)
    v = re.sub(r"[^a-z0-9_]", "_", v)
    v = re.sub(r"_+", "_", v)
    return v.strip("_") or "trafiklab"

SENSOR_DESCRIPTIONS = [
    SensorEntityDescription(
        key="next_departure",
        translation_key="next_departure",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    SensorEntityDescription(
        key="next_arrival",
        translation_key="next_arrival",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Trafiklab sensor based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sensor_type = entry.data.get(CONF_SENSOR_TYPE, "departure")
    if sensor_type == SENSOR_TYPE_RESROBOT:
        description = SensorEntityDescription(
            key="resrobot_travel",
            translation_key="resrobot_travel",
            icon="mdi:train-bus",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
        )
    elif sensor_type == SENSOR_TYPE_ARRIVAL:
        description = SENSOR_DESCRIPTIONS[1]
    else:
        description = SENSOR_DESCRIPTIONS[0]
    async_add_entities(
        [
            TrafikLabSensor(coordinator, entry, description),
            TrafikLabRequestsTodaySensor(coordinator, entry),
        ],
        True,
    )


class TrafikLabSensor(CoordinatorEntity[TrafikLabCoordinator], SensorEntity):
    """Representation of a Trafiklab sensor."""

    def __init__(
        self,
        coordinator: TrafikLabCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        configured_name = (self._entry.data.get(CONF_NAME) or "").strip() or "trafiklab"
        name_slug = _slugify(configured_name)
        stype = self._entry.data.get(CONF_SENSOR_TYPE, "departure")

        if stype == SENSOR_TYPE_ARRIVAL:
            entity_id_base = f"trafiklab_arrival_{name_slug}"
        elif stype == SENSOR_TYPE_RESROBOT:
            entity_id_base = f"trafiklab_travel_{name_slug}"
        else:
            entity_id_base = f"trafiklab_departure_{name_slug}"
        self._attr_suggested_object_id = entity_id_base

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.data.get(CONF_NAME),
            "manufacturer": "Trafiklab",
            "model": "Public Transport"
        }

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        sensor_type = self._entry.data.get(CONF_SENSOR_TYPE, "departure")
        if sensor_type == "resrobot_travel_search":
            # Parse Resrobot response and filter by time window
            trips_raw = self.coordinator.data.get("Trip", [])
            if not trips_raw:
                return None
            # Normalize + sort trips/legs locally as well (in case coordinator didn't)
            options = {**self._entry.options, **self._entry.data}
            max_trip_duration: int | None = options.get(CONF_MAX_TRIP_DURATION)
            trips_sorted = self._normalize_resrobot_trips(trips_raw, max_trip_duration)
            time_window = int(options.get("time_window", 60))
            from datetime import datetime
            now = datetime.now()
            # Find first leg within time window from sorted trips/legs
            for trip in trips_sorted:
                for leg in trip.get("legs", []):
                    origin_time = leg.get("origin_time", "")
                    if not origin_time:
                        continue
                    try:
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                            try:
                                dt = datetime.strptime(origin_time, fmt)
                                break
                            except ValueError:
                                dt = None
                        if not dt:
                            continue
                        minutes_until = int((dt - now).total_seconds() / 60)
                        if 0 <= minutes_until <= time_window:
                            return minutes_until
                    except Exception as err:
                        _LOGGER.debug("Resrobot time parse error: %s", err)
                        continue
            return None
        items = self._get_data_items()
        if not items:
            return None
        first_item = items[0]
        scheduled_time = first_item.get("scheduled", "")
        realtime_time = first_item.get("realtime", "")
        departure_time = realtime_time or scheduled_time
        if not departure_time:
            return None
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(departure_time)
            # Handle timezone-aware vs naive datetimes safely
            if dt.tzinfo is not None:
                now = datetime.now(dt.tzinfo)
            else:
                now = datetime.now()
            return int((dt - now).total_seconds() / 60)
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("Time parse error: %s", err)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        api_error = getattr(self.coordinator, "last_api_error", None)
        if not self.coordinator.data:
            attrs: dict[str, Any] = {}
            self._inject_api_error(attrs)
            return attrs
        sensor_type = self._entry.data.get(CONF_SENSOR_TYPE, "departure")
        if sensor_type == "resrobot_travel_search":
            # Build a sorted top-level trips array and per-trip sorted legs array
            trips_raw = self.coordinator.data.get("Trip", [])
            if not trips_raw:
                attrs = {}
                self._inject_api_error(attrs)
                return attrs
            options_merged = {**self._entry.options, **self._entry.data}
            max_trip_duration: int | None = options_merged.get(CONF_MAX_TRIP_DURATION)
            trips_sorted = self._normalize_resrobot_trips(trips_raw, max_trip_duration)
            attrs: dict[str, Any] = {
                "num_trips": len(trips_sorted),
                "trips": trips_sorted,
                "attribution": "Data from Resrobot/Trafiklab.se",
                "last_update": getattr(self.coordinator, "last_successful_update", None),
                "integration": DOMAIN,
            }
            self._inject_api_error(attrs)
            return attrs
        # ...existing code for departure/arrival...
        items = self._get_data_items()
        if not items:
            return {}
        first_item = items[0]
        merged_cfg = {**self._entry.data, **self._entry.options}
        configured_direction = merged_cfg.get(CONF_DIRECTION, "")
        max_deps, by_mode = departure_targets_from_config(merged_cfg)
        _, by_direction = direction_targets_from_config(merged_cfg)
        limited_items = (
            _limit_items_by_direction(items, max_deps, by_direction)
            if by_direction
            else _limit_items_by_mode(items, max_deps, by_mode)
        )
        attrs = {
            "line": (first_item.get("route") or {}).get("designation", ""),
            "destination": (first_item.get("route") or {}).get("direction", ""),
            "direction": configured_direction,
            "scheduled_time": first_item.get("scheduled", ""),
            "expected_time": first_item.get("realtime", ""),
            "transport_mode": (first_item.get("route") or {}).get("transport_mode", ""),
            "real_time": first_item.get("is_realtime", False),
            "delay": first_item.get("delay", 0),
            "canceled": first_item.get("canceled"),
            "platform": (first_item.get("realtime_platform") or {}).get("designation", ""),
            "upcoming": self._build_upcoming_array(limited_items),
            "attribution": "Data from Trafiklab.se",
            "last_update": getattr(self.coordinator, "last_successful_update", None),
            "integration": DOMAIN,
        }
        self._inject_api_error(attrs)
        return attrs

    def _inject_api_error(self, attrs: dict[str, Any]) -> None:
        """Inject api_error_code/api_error_message into attrs when coordinator has an error."""
        api_error = getattr(self.coordinator, "last_api_error", None)
        if api_error:
            attrs["api_error_code"] = api_error.get("code")
            attrs["api_error_message"] = api_error.get("message")

    def _build_upcoming_array(self, items: list[dict]) -> list[dict]:
        from datetime import datetime

        upcoming: list[dict] = []
        for idx, item in enumerate(items):
            scheduled_time = item.get("scheduled", "")
            realtime_time = item.get("realtime", "")
            departure_time = realtime_time or scheduled_time
            minutes_until = None
            time_formatted = ""
            if departure_time:
                try:
                    dt = datetime.fromisoformat(departure_time)
                    if dt.tzinfo is not None:
                        now = datetime.now(dt.tzinfo)
                    else:
                        now = datetime.now()
                    minutes_until = int((dt - now).total_seconds() / 60)
                    time_formatted = dt.strftime("%H:%M")
                except Exception:  # pragma: no cover
                    pass
            notices = _notices_from_alerts(item)
            entry: dict = {
                    "index": idx,
                    "line": (item.get("route") or {}).get("designation", "") or "Unknown",
                    "destination": (item.get("route") or {}).get("direction", "") or "Unknown",
                    "time_formatted": time_formatted,
                    "minutes_until": minutes_until,
                    "transport_mode": (item.get("route") or {}).get("transport_mode", "") or "Unknown",
                    "real_time": bool(item.get("is_realtime", False)),
                    "delay_minutes": int(item.get("delay", 0) / 60) if item.get("delay") else 0,
                    "canceled": bool(item.get("canceled")),
                    "platform": (item.get("realtime_platform") or {}).get("designation", "")
                    or (item.get("scheduled_platform") or {}).get("designation", ""),
                    "trip_id": (item.get("trip") or {}).get("trip_id", "") or "",
                    "has_notices": bool(notices),
                }
            if notices:
                entry["notices"] = notices
            agency = (item.get("agency") or {}).get("name", "")
            if agency:
                entry["agency"] = agency
            route_name = (item.get("route") or {}).get("name", "")
            if route_name:
                entry["route_name"] = route_name
            upcoming.append(entry)
        return upcoming

    @staticmethod
    def _normalize_resrobot_trips(trips_raw: Any, max_trip_duration: int | None = None) -> list[dict[str, Any]]:
        """Normalize and sort ResRobot trips and their legs for attribute exposure.

        Returns a list of trips where each has a key "legs" which is a list of
        simplified leg dicts, sorted by origin datetime. The trips list itself
        is sorted by the first leg's origin datetime.
        """
        from datetime import datetime

        # Ensure list at top level
        if isinstance(trips_raw, dict):
            trips_iter = [trips_raw]
        else:
            trips_iter = list(trips_raw or [])

        def parse_dt(date_str: str, time_str: str) -> datetime | None:
            if not date_str or not time_str:
                return None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(f"{date_str} {time_str}", fmt)
                except ValueError:
                    continue
            return None

        # Minimal fallback mapping; prefer Product-provided labels when available
        # Add as needed from list: https://www.trafiklab.se/api/our-apis/resrobot-v21/common/
        category_map = {
            "BLT": "Bus",
            "BRE": "Bus",
            "BBL": "Bus",
            "BXB": "Express Bus",
            "BAX": "Airport Express Bus",
            "BRB": "Replacement Bus",
            "TRN": "Train",
            "JRE": "Train",
            "JIC": "Train",
            "JST": "Train",
            "JEX": "Train",
            "JBL": "Train",
            "JLT": "Train",
            "JEN": "EuroNight Train",
            "JNT": "Night Train",
            "JAX": "Airport Express Train",
            "SLT": "Tram",
            "ULT": "Metro",
            "FLT": "Ferry",
        }

        # Map leg.type codes to human-friendly strings
        type_map = {
            "JNY": "Public Transport",
            "TRSF": "Transfer",
            "WALK": "Walk to/from",
        }

        def translate_category(abbrev: str | None) -> str:
            """Translate the API category code via category_map; fallback to raw code."""
            if not abbrev:
                return ""
            key = str(abbrev).upper()
            return category_map.get(key, str(abbrev))

        def translate_type(raw_type: str | None) -> str:
            if not raw_type:
                return ""
            key = str(raw_type).upper()
            return type_map.get(key, str(raw_type))

        def parse_iso_duration_minutes(dur: Any) -> int | None:
            """Parse ISO8601 duration (e.g., PT1H30M, PT9M, PT45S) into integer minutes."""
            if not isinstance(dur, str) or not dur:
                return None
            # Basic ISO8601 duration parser focusing on time part; supports days as well.
            import re
            # Split date and time components
            # Example: P1DT2H3M4S, PT45M, PT1H, PT30S
            pattern = re.compile(
                r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
                re.IGNORECASE,
            )
            m = pattern.match(dur)
            if not m:
                return None
            weeks = int(m.group("weeks") or 0)
            days = int(m.group("days") or 0)
            hours = int(m.group("hours") or 0)
            minutes = int(m.group("minutes") or 0)
            seconds = int(m.group("seconds") or 0)
            total_minutes = weeks * 7 * 24 * 60 + days * 24 * 60 + hours * 60 + minutes + (seconds // 60)
            return total_minutes

        trips_out: list[dict[str, Any]] = []
        for idx, trip in enumerate(trips_iter):
            leg_container = (trip or {}).get("LegList", {}) or {}
            legs = leg_container.get("Leg")
            if legs is None:
                legs_list = []
            elif isinstance(legs, dict):
                legs_list = [legs]
            else:
                legs_list = list(legs)

            simplified_legs: list[dict[str, Any]] = []
            for leg in legs_list:
                origin = (leg or {}).get("Origin", {}) or {}
                dest = (leg or {}).get("Destination", {}) or {}
                product = (leg or {}).get("Product", {})
                if isinstance(product, list):
                    product_obj = product[0] if product else {}
                else:
                    product_obj = product or {}
                # Determine translated category from leg.category only
                category_abbrev = (leg or {}).get("category")
                category_full = translate_category(category_abbrev)
                # Determine translated type
                raw_type = (leg or {}).get("type")
                type_full = translate_type(raw_type)
                # Determine duration in minutes (prefer leg.duration, fallback to GisRoute.durS)
                dur_iso = (leg or {}).get("duration") or ((leg or {}).get("GisRoute") or {}).get("durS")
                duration_minutes = parse_iso_duration_minutes(dur_iso)

                leg_dict = {
                    "origin_name": origin.get("name", ""),
                    "origin_time": f"{origin.get('date', '')} {origin.get('time', '')}".strip(),
                    "dest_name": dest.get("name", ""),
                    "dest_time": f"{dest.get('date', '')} {dest.get('time', '')}".strip(),
                    # Apply translation so 'type' is human readable (Public Transport/Transfer/Walk to/from)
                    "type": type_full,
                    "product": product_obj.get("name", ""),
                    "direction": leg.get("direction", ""),
                    "distance": leg.get("dist"),
                    "line_number": leg.get("number") or product_obj.get("num") or product_obj.get("displayNumber"),
                    "duration": duration_minutes,
                    # Apply translation for category using Product labels or category_map fallback
                    "category": category_full,
                    # Platform from Timetable API cross-check (empty when not enriched or no match)
                    "platform": leg.get("_realtime_platform", ""),
                }
                # attach parsed dt for sorting
                leg_dt = parse_dt(origin.get("date", ""), origin.get("time", ""))
                leg_dict["_dt"] = leg_dt
                leg_dict["_idx"] = leg.get("idx", 0)
                simplified_legs.append(leg_dict)

            # Sort legs by dt then idx for stability
            simplified_legs.sort(key=lambda x: (x.get("_dt") or datetime.max, x.get("_idx", 0)))
            # Drop helper keys
            for lg in simplified_legs:
                lg.pop("_dt", None)
                lg.pop("_idx", None)

            # Trip-level key for sorting
            first_leg_dt = None
            if simplified_legs:
                # Reparse origin_time to dt for trip key
                ot = simplified_legs[0].get("origin_time", "")
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        first_leg_dt = datetime.strptime(ot, fmt)
                        break
                    except ValueError:
                        continue
            if first_leg_dt is None:
                # fallback to trip.Origin
                torg = (trip or {}).get("Origin", {}) or {}
                first_leg_dt = parse_dt(torg.get("date", ""), torg.get("time", "")) or datetime.max

            # Compute total trip duration: first leg origin_time → last leg dest_time
            duration_total: int | None = None
            if simplified_legs:
                first_leg = simplified_legs[0]
                last_leg = simplified_legs[-1]
                origin_dt = parse_dt(
                    *first_leg.get("origin_time", " ").split(" ", 1)
                ) if " " in first_leg.get("origin_time", "") else None
                dest_str = last_leg.get("dest_time", "")
                dest_dt: datetime | None = None
                if dest_str:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            dest_dt = datetime.strptime(dest_str, fmt)
                            break
                        except ValueError:
                            continue
                if origin_dt is not None and dest_dt is not None:
                    duration_total = max(0, int((dest_dt - origin_dt).total_seconds() / 60))

            # Filter by max_trip_duration when set (None = no limit, backward compatible)
            if (
                max_trip_duration is not None
                and duration_total is not None
                and duration_total > max_trip_duration
            ):
                continue

            trips_out.append({
                "index": idx,
                "legs": simplified_legs,
                "duration_total": duration_total,
                "_dt": first_leg_dt,
            })

        # Sort trips by first leg datetime
        trips_out.sort(key=lambda t: t.get("_dt") or datetime.max)
        for out_idx, tp in enumerate(trips_out):
            tp.pop("_dt", None)
            tp["index"] = out_idx
        return trips_out

    def _get_data_items(self) -> list[dict]:
        data = self.coordinator.data
        if not data:
            return []
        raw_items: list[dict] = []
        if self.entity_description.key == "next_arrival":
            if isinstance(data.get("arrivals"), list):
                raw_items = data["arrivals"]
        else:
            if isinstance(data.get("departures"), list):
                raw_items = data["departures"]
        if not raw_items:
            return []
        merged_cfg = {**self._entry.data, **self._entry.options}
        line_filter = (merged_cfg.get(CONF_LINE_FILTER) or "").strip()
        direction_raw = merged_cfg.get(CONF_DIRECTION) or ""
        transport_modes = merged_cfg.get(CONF_TRANSPORT_MODES) or []
        filtered = filter_timetable_items(
            raw_items,
            line_filter=line_filter,
            direction=direction_raw,
            transport_modes=transport_modes,
        )
        time_window = int(merged_cfg.get(CONF_TIME_WINDOW, DEFAULT_TIME_WINDOW))

        def match_time_window(item: dict) -> bool:
            departure_time = item.get("realtime") or item.get("scheduled") or ""
            if not departure_time:
                return True
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(departure_time)
                if dt.tzinfo is not None:
                    now = datetime.now(dt.tzinfo)
                else:
                    now = datetime.now()
                minutes_until = int((dt - now).total_seconds() / 60)
                return 0 <= minutes_until <= time_window
            except Exception:  # pragma: no cover
                return True

        filtered = [it for it in filtered if match_time_window(it)]
        _LOGGER.debug(
            "Filtered %d -> %d items (lines=%s direction_substr='%s')",
            len(raw_items),
            len(filtered),
            line_filter or "*",
            direction_raw or "*",
        )
        return filtered


class TrafikLabRequestsTodaySensor(CoordinatorEntity[TrafikLabCoordinator], SensorEntity):
    """Diagnostic sensor: real Trafiklab/Resrobot HTTP requests made by this entry.

    Counts every actual outgoing HTTP call (timetable chunks, resrobot
    searches, platform enrichment) regardless of whether it came from the
    scheduled refresh, ``trafiklab.update_now``, or the update button — see
    ``request_stats.py``. Counts persist across restarts and options reloads,
    so tuning options in the UI won't reset today's count mid-debugging.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "api_requests_today"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: TrafikLabCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_api_requests_today"
        configured_name = (self._entry.data.get(CONF_NAME) or "").strip() or "trafiklab"
        self._attr_suggested_object_id = f"trafiklab_{_slugify(configured_name)}_api_requests_today"

    @property
    def available(self) -> bool:
        # Stay available even when the last data fetch failed — the request
        # count itself is still valid and is often exactly what you want to
        # see while an API error is happening.
        return True

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.data.get(CONF_NAME),
            "manufacturer": "Trafiklab",
            "model": "Public Transport",
        }

    @property
    def native_value(self) -> int:
        return self.coordinator.request_stats.today_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self.coordinator.request_stats
        return {
            "total_since_install": stats.total,
            "last_request_at": stats.last_request_at,
            "last_endpoint": stats.last_endpoint,
            "by_endpoint": dict(stats.by_endpoint),
            "recent_requests": list(stats.recent),
        }


normalize_resrobot_trips = TrafikLabSensor._normalize_resrobot_trips
