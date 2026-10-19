"""Constants for the Trafiklab integration."""
from typing import Final

DOMAIN: Final = "trafiklab"

# Configuration keys
CONF_API_KEY: Final = "api_key"
CONF_STOP_ID: Final = "stop_id"
CONF_SITE_ID: Final = "site_id"
CONF_LINE_NUMBER: Final = "line_number"
CONF_LINE_FILTER: Final = "line_filter"
CONF_DIRECTION: Final = "direction"
CONF_TRANSPORT_MODE: Final = "transport_mode"
CONF_SENSOR_TYPE: Final = "sensor_type"
CONF_TIME_WINDOW: Final = "time_window"
CONF_REFRESH_INTERVAL: Final = "refresh_interval"
CONF_UPDATE_CONDITION: Final = "update_condition"
CONF_MAX_DEPARTURES: Final = "max_departures"
CONF_MAX_DEPARTURES_BY_MODE: Final = "max_departures_by_mode"
CONF_MAX_DEPARTURES_BY_DIRECTION: Final = "max_departures_by_direction"
CONF_NIGHT_GAP_BY_MODE: Final = "night_gap_by_mode"
CONF_MAX_TIMETABLE_CHUNKS: Final = "max_timetable_chunks"
CONF_NAME: Final = "name"
# Resrobot-specific config keys
CONF_ORIGIN_TYPE: Final = "origin_type"
CONF_ORIGIN: Final = "origin"
CONF_DESTINATION_TYPE: Final = "destination_type"
CONF_DESTINATION: Final = "destination"
CONF_VIA: Final = "via"
CONF_AVOID: Final = "avoid"
CONF_MAX_WALKING_DISTANCE: Final = "max_walking_distance"
CONF_MAX_TRIP_DURATION: Final = "max_trip_duration"
CONF_TRANSPORT_MODES: Final = "transport_modes"
CONF_INCLUDE_PLATFORM: Final = "include_platform"
CONF_REALTIME_API_KEY: Final = "realtime_api_key"


# Sensor types
SENSOR_TYPE_DEPARTURE: Final = "departure"
SENSOR_TYPE_ARRIVAL: Final = "arrival"
SENSOR_TYPE_RESROBOT: Final = "resrobot_travel_search"

# Default values
DEFAULT_SCAN_INTERVAL: Final = 300  # 5 minutes in seconds
MINIMUM_SCAN_INTERVAL: Final = 60   # 1 minute minimum
DEFAULT_NAME: Final = "Trafiklab"
DEFAULT_TIME_WINDOW: Final = 60  # minutes
# Trafiklab Timetables API returns a fixed 60-minute window per request.
TIMETABLE_API_WINDOW_MINUTES: Final = 60
# SL bus/train night gap defaults (last departures ~02:00, first ~04:00).
DEFAULT_NIGHT_GAP_END_HOUR: Final = 2
DEFAULT_NIGHT_GAP_START_HOUR: Final = 4
DEFAULT_NIGHT_GAP_BY_MODE: Final = {
    "train": {"end": DEFAULT_NIGHT_GAP_END_HOUR, "start": DEFAULT_NIGHT_GAP_START_HOUR},
    "bus": {"end": DEFAULT_NIGHT_GAP_END_HOUR, "start": DEFAULT_NIGHT_GAP_START_HOUR},
}
DEFAULT_UPDATE_CONDITION: Final = ""  # empty means always update
DEFAULT_MAX_DEPARTURES: Final = 10
MIN_MAX_DEPARTURES: Final = 1
MAX_MAX_DEPARTURES: Final = 100
DEFAULT_MAX_TIMETABLE_CHUNKS: Final = 3
MIN_MAX_TIMETABLE_CHUNKS: Final = 1
MAX_MAX_TIMETABLE_CHUNKS: Final = 24


# API endpoints
API_BASE_URL: Final = "https://realtime-api.trafiklab.se/v1"
DEPARTURES_ENDPOINT: Final = "/departures"
ARRIVALS_ENDPOINT: Final = "/arrivals"
STOP_LOOKUP_ENDPOINT: Final = "/stops/name"

# Resrobot API
RESROBOT_BASE_URL: Final = "https://api.resrobot.se/v2.1"
RESROBOT_TRAVEL_SEARCH_ENDPOINT: Final = "/trip"
RESROBOT_LOCATION_ENDPOINT: Final = "/location.name"

# Transport modes (config key → API transport_mode value)
TRANSPORT_MODES = {
    "bus": "BUS",
    "metro": "METRO",
    "train": "TRAIN",
    "tram": "TRAM",
    "boat": "SHIP",
}

# API transport_mode values → config key (includes common aliases)
API_TRANSPORT_MODE_TO_KEY: dict[str, str] = {
    "BUS": "bus",
    "METRO": "metro",
    "TRAIN": "train",
    "TRAM": "tram",
    "SHIP": "boat",
    "BOAT": "boat",
    "FERRY": "boat",
}

# Form field keys for optional per-mode departure limits
CONF_MAX_DEPARTURES_MODE_FIELDS: dict[str, str] = {
    mode: f"max_departures_{mode}" for mode in TRANSPORT_MODES
}

# Form field keys for optional per-mode night service gaps (hour 0–23)
CONF_NIGHT_GAP_END_MODE_FIELDS: dict[str, str] = {
    mode: f"night_gap_end_{mode}" for mode in TRANSPORT_MODES
}
CONF_NIGHT_GAP_START_MODE_FIELDS: dict[str, str] = {
    mode: f"night_gap_start_{mode}" for mode in TRANSPORT_MODES
}

# Resrobot products bitmask per transport mode (request-side filtering)
# Values are additive bitmasks as documented at trafiklab.se/api/our-apis/resrobot-v21/common/
# BUS = 128 (local buses) + 8 (express buses / Flygbussar) = 136
# TRAIN = 2 (high-speed / Arlanda Express) + 4 (regional / IC) + 16 (local trains) = 22
# METRO = 32, TRAM = 64, BOAT = 256 (ferries)
RESROBOT_PRODUCTS_MAP: dict[str, int] = {
    "bus": 136,
    "train": 22,
    "metro": 32,
    "tram": 64,
    "boat": 256,
}

# Service names
SERVICE_STOP_LOOKUP: Final = "stop_lookup"
SERVICE_UPDATE_NOW: Final = "update_now"
SERVICE_TRAVEL_SEARCH: Final = "travel_search"

# Service fields
ATTR_SEARCH_QUERY: Final = "search_query"
ATTR_STOPS_FOUND: Final = "stops_found"

# Attributes
ATTR_STOP_NAME: Final = "stop_name"
ATTR_LINE: Final = "line"
ATTR_DESTINATION: Final = "destination"
ATTR_DIRECTION: Final = "direction"
ATTR_EXPECTED_TIME: Final = "expected_time"
ATTR_REAL_TIME: Final = "real_time"
ATTR_TRANSPORT_MODE: Final = "transport_mode"
ATTR_DEVIATIONS: Final = "deviations"

# Error messages
ERROR_API_KEY_INVALID: Final = "Invalid API key"
ERROR_STOP_NOT_FOUND: Final = "Stop not found"
ERROR_CONNECTION: Final = "Connection error"
