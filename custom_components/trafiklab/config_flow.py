"""Config flow for Trafiklab integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME as HA_CONF_NAME  # avoid collision; not used
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    TrafikLabApiClient,
    TrafikLabApiError,
    TrafikLabAuthError,
    TrafikLabQuotaError,
    TrafikLabNotFoundError,
)

from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_API_KEY,
    CONF_STOP_ID,
    CONF_SITE_ID,
    CONF_LINE_FILTER,
    CONF_DIRECTION,
    CONF_SENSOR_TYPE,
    CONF_TIME_WINDOW,
    CONF_REFRESH_INTERVAL,
    CONF_UPDATE_CONDITION,
    CONF_MAX_DEPARTURES,
    CONF_MAX_DEPARTURES_BY_MODE,
    CONF_MAX_DEPARTURES_BY_DIRECTION,
    CONF_MAX_DEPARTURES_MODE_FIELDS,
    CONF_NIGHT_GAP_BY_MODE,
    CONF_NIGHT_GAP_END_MODE_FIELDS,
    CONF_NIGHT_GAP_START_MODE_FIELDS,
    CONF_MAX_TIMETABLE_CHUNKS,
    DEFAULT_NIGHT_GAP_BY_MODE,
    DEFAULT_TIME_WINDOW,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_MAX_TIMETABLE_CHUNKS,
    MIN_MAX_DEPARTURES,
    MAX_MAX_DEPARTURES,
    MIN_MAX_TIMETABLE_CHUNKS,
    MAX_MAX_TIMETABLE_CHUNKS,
    MINIMUM_SCAN_INTERVAL,
    SENSOR_TYPE_DEPARTURE,
    SENSOR_TYPE_ARRIVAL,
    SENSOR_TYPE_RESROBOT,
    API_BASE_URL,
    DEPARTURES_ENDPOINT,
    ERROR_API_KEY_INVALID,
    ERROR_STOP_NOT_FOUND,
    ERROR_CONNECTION,
    CONF_ORIGIN_TYPE,
    CONF_ORIGIN,
    CONF_DESTINATION_TYPE,
    CONF_DESTINATION,
    CONF_VIA,
    CONF_AVOID,
    CONF_MAX_WALKING_DISTANCE,
    CONF_MAX_TRIP_DURATION,
    CONF_MAX_RESULTS,
    DEFAULT_MAX_RESULTS,
    CONF_TRANSPORT_MODES,
    CONF_INCLUDE_PLATFORM,
    TRANSPORT_MODES,
)

_LOGGER = logging.getLogger(__name__)

_TRANSPORT_MODES_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=["bus", "metro", "train", "tram", "boat"],
        multiple=True,
        mode=SelectSelectorMode.LIST,
        translation_key="transport_modes",
    )
)

_SENSOR_TYPE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[SENSOR_TYPE_DEPARTURE, SENSOR_TYPE_ARRIVAL, SENSOR_TYPE_RESROBOT],
        multiple=False,
        mode=SelectSelectorMode.LIST,
        translation_key="sensor_type",
    )
)

_SENSOR_TYPE_DEP_ARR_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[SENSOR_TYPE_DEPARTURE, SENSOR_TYPE_ARRIVAL],
        multiple=False,
        mode=SelectSelectorMode.LIST,
        translation_key="sensor_type",
    )
)

_LOCATION_TYPE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=["stop_id", "coordinates"],
        multiple=False,
        mode=SelectSelectorMode.LIST,
        translation_key="origin_type",
    )
)

_MAX_DEPARTURES_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_MAX_DEPARTURES,
        max=MAX_MAX_DEPARTURES,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)

# NumberSelector validates as float; coerce to int for storage/slicing.
_MAX_DEPARTURES_SCHEMA = vol.All(_MAX_DEPARTURES_SELECTOR, vol.Coerce(int))

_MAX_TIMETABLE_CHUNKS_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_MAX_TIMETABLE_CHUNKS,
        max=MAX_MAX_TIMETABLE_CHUNKS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
_MAX_TIMETABLE_CHUNKS_SCHEMA = vol.All(_MAX_TIMETABLE_CHUNKS_SELECTOR, vol.Coerce(int))

# Per-mode overrides use 0 as "unset" so Home Assistant always renders the fields.
# (Optional NumberSelector fields with default=None are hidden in the config UI.)
_OPTIONAL_MAX_DEPARTURES_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=MAX_MAX_DEPARTURES,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
_OPTIONAL_MAX_DEPARTURES_SCHEMA = vol.All(
    _OPTIONAL_MAX_DEPARTURES_SELECTOR, vol.Coerce(int)
)

_MAX_DEPARTURES_MODE_SCHEMA: dict[Any, Any] = {
    vol.Optional(field_key, default=0): _OPTIONAL_MAX_DEPARTURES_SCHEMA
    for field_key in CONF_MAX_DEPARTURES_MODE_FIELDS.values()
}

_NIGHT_GAP_HOUR_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=23,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
_NIGHT_GAP_HOUR_SCHEMA = vol.All(_NIGHT_GAP_HOUR_SELECTOR, vol.Coerce(int))

_NIGHT_GAP_FORM_DEFAULTS: dict[str, tuple[int, int]] = {
    mode: (
        DEFAULT_NIGHT_GAP_BY_MODE[mode]["end"],
        DEFAULT_NIGHT_GAP_BY_MODE[mode]["start"],
    )
    if mode in DEFAULT_NIGHT_GAP_BY_MODE
    else (0, 0)
    for mode in TRANSPORT_MODES
}

_NIGHT_GAP_MODE_SCHEMA: dict[Any, Any] = {}
for _mode, (_default_end, _default_start) in _NIGHT_GAP_FORM_DEFAULTS.items():
    _NIGHT_GAP_MODE_SCHEMA[
        vol.Optional(CONF_NIGHT_GAP_END_MODE_FIELDS[_mode], default=_default_end)
    ] = _NIGHT_GAP_HOUR_SCHEMA
    _NIGHT_GAP_MODE_SCHEMA[
        vol.Optional(CONF_NIGHT_GAP_START_MODE_FIELDS[_mode], default=_default_start)
    ] = _NIGHT_GAP_HOUR_SCHEMA


def _pack_max_departures_by_mode(user_input: dict[str, Any]) -> dict[str, int]:
    """Build the stored mode→limit map from flat form fields."""
    by_mode: dict[str, int] = {}
    for mode, field_key in CONF_MAX_DEPARTURES_MODE_FIELDS.items():
        if field_key not in user_input:
            continue
        value = user_input.get(field_key)
        if value is None:
            continue
        parsed = int(value)
        if parsed <= 0:
            continue
        by_mode[mode] = parsed
    return by_mode


def _expand_max_departures_by_mode(values: dict[str, Any]) -> None:
    """Expand stored mode→limit map into flat form fields for suggested values."""
    by_mode = values.get(CONF_MAX_DEPARTURES_BY_MODE) or {}
    if not isinstance(by_mode, dict):
        by_mode = {}
    for mode, field_key in CONF_MAX_DEPARTURES_MODE_FIELDS.items():
        values[field_key] = by_mode.get(mode, 0)


def _pack_night_gap_by_mode(user_input: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Build the stored mode→gap map from flat form fields."""
    by_mode: dict[str, dict[str, int]] = {}
    for mode in TRANSPORT_MODES:
        end_key = CONF_NIGHT_GAP_END_MODE_FIELDS[mode]
        start_key = CONF_NIGHT_GAP_START_MODE_FIELDS[mode]
        if end_key not in user_input and start_key not in user_input:
            continue
        try:
            end = int(user_input.get(end_key, 0))
            start = int(user_input.get(start_key, 0))
        except (TypeError, ValueError):
            continue
        if end <= 0 or start <= end:
            continue
        by_mode[mode] = {"end": end, "start": start}
    return by_mode


def _expand_night_gap_by_mode(values: dict[str, Any]) -> None:
    """Expand stored mode→gap map into flat form fields for suggested values."""
    by_mode = values.get(CONF_NIGHT_GAP_BY_MODE)
    if by_mode is None:
        for mode, (end, start) in _NIGHT_GAP_FORM_DEFAULTS.items():
            values[CONF_NIGHT_GAP_END_MODE_FIELDS[mode]] = end
            values[CONF_NIGHT_GAP_START_MODE_FIELDS[mode]] = start
        return
    if not isinstance(by_mode, dict):
        by_mode = {}
    for mode in TRANSPORT_MODES:
        cfg = by_mode.get(mode) or {}
        values[CONF_NIGHT_GAP_END_MODE_FIELDS[mode]] = int(cfg.get("end", 0))
        values[CONF_NIGHT_GAP_START_MODE_FIELDS[mode]] = int(cfg.get("start", 0))


def _departure_arrival_options_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Extract departure/arrival options from a form submission."""
    return {
        CONF_LINE_FILTER: user_input.get(CONF_LINE_FILTER, ""),
        CONF_DIRECTION: user_input.get(CONF_DIRECTION, ""),
        CONF_TRANSPORT_MODES: user_input.get(CONF_TRANSPORT_MODES, []),
        CONF_TIME_WINDOW: user_input.get(CONF_TIME_WINDOW, DEFAULT_TIME_WINDOW),
        CONF_REFRESH_INTERVAL: user_input.get(CONF_REFRESH_INTERVAL, DEFAULT_SCAN_INTERVAL),
        CONF_UPDATE_CONDITION: user_input.get(CONF_UPDATE_CONDITION, ""),
        CONF_MAX_DEPARTURES: user_input.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES),
        CONF_MAX_DEPARTURES_BY_MODE: _pack_max_departures_by_mode(user_input),
        CONF_MAX_DEPARTURES_BY_DIRECTION: user_input.get(CONF_MAX_DEPARTURES_BY_DIRECTION, ""),
        CONF_NIGHT_GAP_BY_MODE: _pack_night_gap_by_mode(user_input),
        CONF_MAX_TIMETABLE_CHUNKS: user_input.get(
            CONF_MAX_TIMETABLE_CHUNKS, DEFAULT_MAX_TIMETABLE_CHUNKS
        ),
    }


_DEPARTURE_ARRIVAL_OPTIONS_KEYS = (
    CONF_LINE_FILTER,
    CONF_DIRECTION,
    CONF_TRANSPORT_MODES,
    CONF_TIME_WINDOW,
    CONF_REFRESH_INTERVAL,
    CONF_UPDATE_CONDITION,
    CONF_MAX_DEPARTURES,
    CONF_MAX_DEPARTURES_BY_MODE,
    CONF_MAX_DEPARTURES_BY_DIRECTION,
    CONF_NIGHT_GAP_BY_MODE,
    CONF_MAX_TIMETABLE_CHUNKS,
)


STEP_SENSOR_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR_TYPE, default=SENSOR_TYPE_DEPARTURE): _SENSOR_TYPE_DEP_ARR_SELECTOR,
        vol.Optional(CONF_LINE_FILTER, default=""): str,
    # Direction is now a free text destination filter (substring match). Keep key name for backward compatibility.
    vol.Optional(CONF_DIRECTION, default=""): str,
        vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1440)
        ),
        vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)
        ),
    # New: Optional Jinja template string to decide whether to perform update. When template renders to 'true' (case-insensitive), update is performed.
        vol.Optional(CONF_UPDATE_CONDITION, default=""): str,
        vol.Optional(CONF_TRANSPORT_MODES, default=[]): _TRANSPORT_MODES_SELECTOR,
        vol.Optional(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _MAX_DEPARTURES_SCHEMA,
        **_MAX_DEPARTURES_MODE_SCHEMA,
        vol.Optional(CONF_MAX_DEPARTURES_BY_DIRECTION, default=""): str,
        vol.Optional(
            CONF_MAX_TIMETABLE_CHUNKS, default=DEFAULT_MAX_TIMETABLE_CHUNKS
        ): _MAX_TIMETABLE_CHUNKS_SCHEMA,
        **_NIGHT_GAP_MODE_SCHEMA,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Trafiklab."""

    VERSION = 2


    def __init__(self):
        """Initialize config flow."""
        self._api_key = None
        self._stop_id = None
        self._name = None
        self._sensor_type = None


    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: sensor type and API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
                # Legacy path: allow initial form to accept api_key + stop_id (+ name)
                if CONF_STOP_ID in user_input and CONF_API_KEY in user_input and CONF_SENSOR_TYPE not in user_input:
                    self._api_key = user_input[CONF_API_KEY]
                    self._stop_id = user_input[CONF_STOP_ID]
                    self._name = ""
                    try:
                        await validate_input(self.hass, {
                            CONF_API_KEY: self._api_key,
                            CONF_STOP_ID: self._stop_id,
                        })
                    except InvalidApiKey:
                        errors["api_key"] = "invalid_api_key"
                    except InvalidStopId:
                        errors["stop_id"] = "invalid_stop_id"
                    except QuotaExceeded:
                        errors["base"] = "quota_exceeded"
                    except CannotConnect:
                        errors["base"] = "cannot_connect"
                    except Exception as err:  # pragma: no cover
                        _LOGGER.exception("Unexpected exception during validation: %s", err)
                        errors["base"] = "unknown"
                    else:
                        # Proceed to sensor configuration step
                        return await self.async_step_sensor()
                else:
                    # New path: choose sensor type first
                    self._sensor_type = user_input[CONF_SENSOR_TYPE]
                    self._api_key = user_input[CONF_API_KEY]
                    self._name = user_input.get(CONF_NAME, "")
                    # Next step depends on sensor type
                    if self._sensor_type in [SENSOR_TYPE_DEPARTURE, SENSOR_TYPE_ARRIVAL]:
                        return await self.async_step_departure_arrival()
                    elif self._sensor_type == SENSOR_TYPE_RESROBOT:
                        return await self.async_step_resrobot()

        # Show initial form
        dynamic_schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_TYPE, default=SENSOR_TYPE_RESROBOT): _SENSOR_TYPE_SELECTOR,
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_NAME, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=dynamic_schema,
            errors=errors,
        )

    async def async_step_departure_arrival(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step for Departure/Arrival sensor config."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._stop_id = user_input[CONF_STOP_ID]
            # Validate input as before
            try:
                await validate_input(self.hass, {
                    CONF_API_KEY: self._api_key,
                    CONF_STOP_ID: self._stop_id,
                })
            except InvalidApiKey:
                errors["api_key"] = "invalid_api_key"
            except InvalidStopId:
                errors["stop_id"] = "invalid_stop_id"
            except QuotaExceeded:
                errors["base"] = "quota_exceeded"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception during validation: %s", err)
                errors["base"] = "unknown"
            else:
                # Combine config data
                base_data = {
                    CONF_API_KEY: self._api_key,
                    CONF_STOP_ID: self._stop_id,
                    CONF_NAME: self._name,
                    CONF_SENSOR_TYPE: self._sensor_type,
                }
                options_data = _departure_arrival_options_from_input(user_input)
                unique_id = f"{self._stop_id}_{self._sensor_type}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                sensor_type_name = "Departures" if self._sensor_type == SENSOR_TYPE_DEPARTURE else "Arrivals"
                title = f"{self._name} {sensor_type_name}".strip()
                return self.async_create_entry(title=title, data=base_data, options=options_data)

        # Show form for Departure/Arrival sensor config
        return self.async_show_form(
            step_id="departure_arrival",
            data_schema=vol.Schema({
                vol.Required(CONF_STOP_ID): str,
                vol.Optional(CONF_LINE_FILTER, default=""): str,
                vol.Optional(CONF_DIRECTION, default=""): str,
                vol.Optional(CONF_TRANSPORT_MODES, default=[]): _TRANSPORT_MODES_SELECTOR,
                vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1440)
                ),
                vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)
                ),
                vol.Optional(CONF_UPDATE_CONDITION, default=""): str,
                vol.Optional(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _MAX_DEPARTURES_SCHEMA,
                **_MAX_DEPARTURES_MODE_SCHEMA,
                vol.Optional(CONF_MAX_DEPARTURES_BY_DIRECTION, default=""): str,
                vol.Optional(
                    CONF_MAX_TIMETABLE_CHUNKS, default=DEFAULT_MAX_TIMETABLE_CHUNKS
                ): _MAX_TIMETABLE_CHUNKS_SCHEMA,
                **_NIGHT_GAP_MODE_SCHEMA,
            }),
            errors=errors,
        )

    async def async_step_resrobot(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step for Resrobot Travel Search sensor config."""
        errors: dict[str, str] = {}

        # Define all required fields for Resrobot, including update frequency and time window
        # Keep options simple; labels are translated via step strings
        resrobot_schema = vol.Schema({
            vol.Required(CONF_ORIGIN_TYPE, default="stop_id"): _LOCATION_TYPE_SELECTOR,
            vol.Required(CONF_ORIGIN, default=""): str,
            vol.Required(CONF_DESTINATION_TYPE, default="stop_id"): _LOCATION_TYPE_SELECTOR,
            vol.Required(CONF_DESTINATION, default=""): str,
            vol.Optional(CONF_VIA, default=""): str,
            vol.Optional(CONF_AVOID, default=""): str,
            vol.Optional(CONF_MAX_WALKING_DISTANCE, default=1000): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
            vol.Optional(CONF_MAX_TRIP_DURATION, default=None): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=1, max=1440))),
            vol.Optional(CONF_TRANSPORT_MODES, default=[]): _TRANSPORT_MODES_SELECTOR,
            vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)),
            vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            vol.Optional(CONF_INCLUDE_PLATFORM, default=False): bool,
            vol.Optional(CONF_MAX_RESULTS, default=DEFAULT_MAX_RESULTS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        })

        if user_input is not None:
            # Validate origin/destination types and values
            origin_type = user_input["origin_type"]
            destination_type = user_input["destination_type"]
            origin = user_input["origin"]
            destination = user_input["destination"]
            via = user_input.get("via", "")
            avoid = user_input.get("avoid", "")
            max_walking_distance = user_input.get("max_walking_distance", 1000)
            max_trip_duration = user_input.get(CONF_MAX_TRIP_DURATION)
            refresh_interval = user_input.get("refresh_interval", DEFAULT_SCAN_INTERVAL)
            time_window = user_input.get("time_window", DEFAULT_TIME_WINDOW)

            # Basic validation for coordinates
            def valid_coords(val):
                try:
                    lat, lon = val.split(",")
                    float(lat)
                    float(lon)
                    return True
                except Exception:
                    return False

            if origin_type == "coordinates" and not valid_coords(origin):
                errors["origin"] = "invalid_coordinates"
            if destination_type == "coordinates" and not valid_coords(destination):
                errors["destination"] = "invalid_coordinates"

            if not errors:
                # Probe Resrobot API to validate the key before saving
                from homeassistant.helpers.aiohttp_client import async_get_clientsession
                _probe_client = TrafikLabApiClient(
                    self._api_key, session=async_get_clientsession(self.hass)
                )
                try:
                    await _probe_client.search_resrobot_stops("Stockholm", self._api_key)
                except TrafikLabAuthError:
                    errors["base"] = "invalid_api_key"
                except TrafikLabQuotaError:
                    errors["base"] = "quota_exceeded"
                except TrafikLabApiError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pragma: no cover
                    errors["base"] = "cannot_connect"
                finally:
                    await _probe_client.close()

            if not errors:
                # Store config and create entry
                base_data = {
                    CONF_API_KEY: self._api_key,
                    CONF_NAME: self._name,
                    CONF_SENSOR_TYPE: self._sensor_type,
                    "origin_type": origin_type,
                    "origin": origin,
                    "destination_type": destination_type,
                    "destination": destination,
                }
                options_data = {
                    "via": via,
                    "avoid": avoid,
                    "max_walking_distance": max_walking_distance,
                    CONF_MAX_TRIP_DURATION: user_input.get(CONF_MAX_TRIP_DURATION),
                    CONF_TRANSPORT_MODES: user_input.get(CONF_TRANSPORT_MODES, []),
                    "refresh_interval": refresh_interval,
                    "time_window": time_window,
                    CONF_INCLUDE_PLATFORM: user_input.get(CONF_INCLUDE_PLATFORM, False),
                    CONF_MAX_RESULTS: user_input.get(CONF_MAX_RESULTS, DEFAULT_MAX_RESULTS),
                }
                # Unique ID should be stable and not include name which can change
                unique_id = f"resrobot_{origin}_{destination}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                title = f"{self._name} Resrobot Travel Search".strip()
                return self.async_create_entry(title=title, data=base_data, options=options_data)

        # Show form for Resrobot Travel Search config
        return self.async_show_form(
            step_id="resrobot",
            data_schema=resrobot_schema,
            errors=errors,
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the sensor configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Combine all configuration data
            # Immutable/base data
            base_data = {
                CONF_API_KEY: self._api_key,
                CONF_STOP_ID: self._stop_id,
                CONF_NAME: self._name,
                CONF_SENSOR_TYPE: user_input[CONF_SENSOR_TYPE],
            }
            # Mutable settings go to options to avoid redundancy
            options_data = _departure_arrival_options_from_input(user_input)

            # Create a unique ID for this sensor configuration
            unique_id = f"{self._stop_id}_{user_input[CONF_SENSOR_TYPE]}"
            if user_input.get(CONF_LINE_FILTER):
                unique_id += f"_{user_input[CONF_LINE_FILTER].replace(',', '_')}"
            if user_input.get(CONF_DIRECTION):
                unique_id += f"_dir{user_input[CONF_DIRECTION]}"

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            # Create title with sensor details
            sensor_type_name = "Departures" if user_input[CONF_SENSOR_TYPE] == SENSOR_TYPE_DEPARTURE else "Arrivals"
            title = f"{self._name} {sensor_type_name}".strip()
            if user_input.get(CONF_LINE_FILTER):
                title += f" (Lines: {user_input[CONF_LINE_FILTER]})"

            return self.async_create_entry(title=title, data=base_data, options=options_data)

        return self.async_show_form(
            step_id="sensor", 
            data_schema=STEP_SENSOR_DATA_SCHEMA, 
            errors=errors,
            description_placeholders={
                "stop_id": self._stop_id,
                "stop_name": self._name,
            }
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration of an existing entry (change data fields)."""
        entry = self._get_reconfigure_entry()
        sensor_type = entry.data.get(CONF_SENSOR_TYPE)
        if sensor_type == SENSOR_TYPE_RESROBOT:
            return await self.async_step_reconfigure_resrobot(user_input)
        return await self.async_step_reconfigure_departure_arrival(user_input)

    async def async_step_reconfigure_departure_arrival(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure API key and stop ID for a departure/arrival sensor."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, {
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_STOP_ID: user_input[CONF_STOP_ID],
                })
            except InvalidApiKey:
                errors["api_key"] = "invalid_api_key"
            except InvalidStopId:
                errors["stop_id"] = "invalid_stop_id"
            except QuotaExceeded:
                errors["base"] = "quota_exceeded"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception during reconfigure validation: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **user_input},
                )

        schema = vol.Schema({
            vol.Required(CONF_API_KEY, default=entry.data.get(CONF_API_KEY, "")): str,
            vol.Required(CONF_STOP_ID, default=entry.data.get(CONF_STOP_ID, "")): str,
        })
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure_resrobot(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure API key and trip endpoints for a Resrobot sensor."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        def valid_coords(val: str) -> bool:
            try:
                lat, lon = val.split(",")
                float(lat)
                float(lon)
                return True
            except Exception:
                return False

        if user_input is not None:
            if user_input.get(CONF_ORIGIN_TYPE) == "coordinates" and not valid_coords(user_input.get(CONF_ORIGIN, "")):
                errors[CONF_ORIGIN] = "invalid_coordinates"
            if user_input.get(CONF_DESTINATION_TYPE) == "coordinates" and not valid_coords(user_input.get(CONF_DESTINATION, "")):
                errors[CONF_DESTINATION] = "invalid_coordinates"

            if not errors:
                # Probe Resrobot API to validate the key before saving
                from homeassistant.helpers.aiohttp_client import async_get_clientsession
                _probe_client = TrafikLabApiClient(
                    user_input[CONF_API_KEY], session=async_get_clientsession(self.hass)
                )
                try:
                    await _probe_client.search_resrobot_stops("Stockholm", user_input[CONF_API_KEY])
                except TrafikLabAuthError:
                    errors["base"] = "invalid_api_key"
                except TrafikLabQuotaError:
                    errors["base"] = "quota_exceeded"
                except TrafikLabApiError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pragma: no cover
                    errors["base"] = "cannot_connect"
                finally:
                    await _probe_client.close()

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **user_input},
                )

        schema = vol.Schema({
            vol.Required(CONF_API_KEY, default=entry.data.get(CONF_API_KEY, "")): str,
            vol.Required(CONF_ORIGIN_TYPE, default=entry.data.get(CONF_ORIGIN_TYPE, "stop_id")): _LOCATION_TYPE_SELECTOR,
            vol.Required(CONF_ORIGIN, default=entry.data.get(CONF_ORIGIN, "")): str,
            vol.Required(CONF_DESTINATION_TYPE, default=entry.data.get(CONF_DESTINATION_TYPE, "stop_id")): _LOCATION_TYPE_SELECTOR,
            vol.Required(CONF_DESTINATION, default=entry.data.get(CONF_DESTINATION, "")): str,
        })
        return self.async_show_form(
            step_id="reconfigure_resrobot",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):  # type: ignore[override]
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for existing entries."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:  # noqa: D401
        is_resrobot = self._entry.data.get(CONF_SENSOR_TYPE) == SENSOR_TYPE_RESROBOT

        if is_resrobot:
            return await self.async_step_init_resrobot(user_input)

        if user_input is not None:
            merged_options = dict(self._entry.options)
            for key in _DEPARTURE_ARRIVAL_OPTIONS_KEYS:
                if key in (CONF_MAX_DEPARTURES_BY_MODE, CONF_NIGHT_GAP_BY_MODE):
                    continue
                if key in user_input:
                    merged_options[key] = user_input[key]
            merged_options[CONF_MAX_DEPARTURES_BY_MODE] = _pack_max_departures_by_mode(
                user_input
            )
            merged_options[CONF_NIGHT_GAP_BY_MODE] = _pack_night_gap_by_mode(
                user_input
            )
            return self.async_create_entry(title="", data=merged_options)

        schema = vol.Schema({
            vol.Optional(CONF_LINE_FILTER, default=""): str,
            vol.Optional(CONF_DIRECTION, default=""): str,
            vol.Optional(CONF_TRANSPORT_MODES, default=[]): _TRANSPORT_MODES_SELECTOR,
            vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=1440)
            ),
            vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)
            ),
            vol.Optional(CONF_UPDATE_CONDITION, default=""): str,
            vol.Optional(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _MAX_DEPARTURES_SCHEMA,
            **_MAX_DEPARTURES_MODE_SCHEMA,
            vol.Optional(CONF_MAX_DEPARTURES_BY_DIRECTION, default=""): str,
            vol.Optional(
                CONF_MAX_TIMETABLE_CHUNKS, default=DEFAULT_MAX_TIMETABLE_CHUNKS
            ): _MAX_TIMETABLE_CHUNKS_SCHEMA,
            **_NIGHT_GAP_MODE_SCHEMA,
        })

        current_values = {**self._entry.data, **self._entry.options}
        # Normalize transport_modes: old entries may lack the key, have None stored,
        # or contain values no longer in the valid set (e.g. the old "ship" key).
        # add_suggested_values_to_schema passes whatever is here to the frontend as
        # suggested_value; invalid/null elements cause "value must be one of [...]".
        current_values[CONF_TRANSPORT_MODES] = [
            m for m in (current_values.get(CONF_TRANSPORT_MODES) or [])
            if isinstance(m, str) and m in TRANSPORT_MODES
        ]
        _expand_max_departures_by_mode(current_values)
        _expand_night_gap_by_mode(current_values)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current_values),
        )

    async def async_step_init_resrobot(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Options step for Resrobot Travel Search sensors."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_VIA, default=""): str,
            vol.Optional(CONF_AVOID, default=""): str,
            vol.Optional(CONF_MAX_WALKING_DISTANCE, default=1000): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=10000)
            ),
            vol.Optional(CONF_MAX_TRIP_DURATION, default=None): vol.Any(
                None, vol.All(vol.Coerce(int), vol.Range(min=1, max=1440))
            ),
            vol.Optional(CONF_TRANSPORT_MODES, default=[]): _TRANSPORT_MODES_SELECTOR,
            vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)
            ),
            vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=1440)
            ),
            vol.Optional(CONF_INCLUDE_PLATFORM, default=False): bool,
            vol.Optional(CONF_MAX_RESULTS, default=DEFAULT_MAX_RESULTS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        })
        current_values = {**self._entry.data, **self._entry.options}
        # Normalize transport_modes: old entries may lack the key, have None stored,
        # or contain values no longer in the valid set (e.g. the old "ship" key).
        # add_suggested_values_to_schema passes whatever is here to the frontend as
        # suggested_value; invalid/null elements cause "value must be one of [...]".
        current_values[CONF_TRANSPORT_MODES] = [
            m for m in (current_values.get(CONF_TRANSPORT_MODES) or [])
            if isinstance(m, str) and m in {"bus", "metro", "train", "tram", "boat"}
        ]
        return self.async_show_form(
            step_id="init_resrobot",
            data_schema=self.add_suggested_values_to_schema(schema, current_values),
        )


# ---------------------------------------------------------------------------
# Validation helpers & custom exceptions (expected by tests)
# ---------------------------------------------------------------------------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidApiKey(HomeAssistantError):
    """Error to indicate the API key is invalid."""


class InvalidStopId(HomeAssistantError):
    """Error to indicate the stop id is invalid."""


class QuotaExceeded(HomeAssistantError):
    """Error to indicate the API quota has been exceeded."""


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Returns minimal info dict. Raises custom exceptions on failure.
    Tests patch this function, so keep signature & return shape stable.
    """
    api_key = data[CONF_API_KEY]
    stop_id = data[CONF_STOP_ID]

    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    client = TrafikLabApiClient(api_key, session=async_get_clientsession(hass))
    try:
        # Attempt a lightweight departures fetch to validate both key & stop.
        await client.get_departures(stop_id)
    except TrafikLabAuthError as err:
        raise InvalidApiKey from err
    except TrafikLabNotFoundError as err:
        raise InvalidStopId from err
    except TrafikLabQuotaError as err:
        raise QuotaExceeded from err
    except TrafikLabApiError as err:
        raise CannotConnect from err
    except Exception as err:  # pragma: no cover - network/aio edge case
        raise CannotConnect from err
    finally:
        await client.close()

    # Title used by tests when patched; we mirror behavior.
    return {"title": data.get(CONF_NAME, "")}
