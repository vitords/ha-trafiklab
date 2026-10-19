from __future__ import annotations
# pyright: reportMissingImports=false, reportGeneralTypeIssues=false
import copy
from datetime import datetime
import pytest

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trafiklab.const import DOMAIN
from custom_components.trafiklab.coordinator import (
    _effective_night_gap,
    _normalize_night_gap_by_mode,
    _timetable_query_times,
)


@pytest.mark.asyncio
async def test_coordinator_respects_update_condition(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k", "stop_id": "740098000", "name": "X", "sensor_type": "departure"},
        options={"update_condition": "false"},
        unique_id="u3",
    )
    entry.add_to_hass(hass)

    # If update condition renders false, coordinator should not call API and data remains None/{} without error
    with patch("custom_components.trafiklab.api.TrafikLabApiClient.get_departures") as mocked:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not mocked.called


@pytest.mark.asyncio
async def test_coordinator_extended_time_window_fetches_multiple_chunks(
    hass: HomeAssistant,
) -> None:
    """time_window > 60 must trigger additional Timetables API requests."""
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "stop_id": "740098000",
            "name": "Extended",
            "sensor_type": "departure",
        },
        options={
            "time_window": 150,
            "refresh_interval": 300,
            # Metro runs outside the train/bus night gap; keep legacy chunk count.
            "transport_modes": ["metro"],
        },
        unique_id="coord-extended-window",
    )
    entry.add_to_hass(hass)

    chunk_a = {
        "departures": [
            {
                "scheduled": datetime.now().isoformat(),
                "realtime": datetime.now().isoformat(),
                "trip_id": "a1",
                "route": {"designation": "40", "transport_mode": "TRAIN"},
            }
        ]
    }
    chunk_b = {
        "departures": [
            {
                "scheduled": (datetime.now() + timedelta(minutes=90)).isoformat(),
                "realtime": (datetime.now() + timedelta(minutes=90)).isoformat(),
                "trip_id": "b1",
                "route": {"designation": "41", "transport_mode": "TRAIN"},
            }
        ]
    }
    mocked = AsyncMock(side_effect=[chunk_a, chunk_b, chunk_b])

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        mocked,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mocked.call_count == 3
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.data.get("departures", [])) == 2


def _dep(trip_id: str, mode: str, minutes: int = 10) -> dict:
    from datetime import datetime, timedelta

    when = (datetime.now() + timedelta(minutes=minutes)).isoformat()
    return {
        "scheduled": when,
        "realtime": when,
        "trip_id": trip_id,
        "route": {"designation": "1", "direction": "Centrum", "transport_mode": mode},
        "trip": {"trip_id": trip_id},
    }


@pytest.mark.asyncio
async def test_coordinator_early_stops_when_mode_limits_filled(
    hass: HomeAssistant,
) -> None:
    """Stop fetching more chunks once per-mode limits are satisfied."""
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "stop_id": "740098000",
            "name": "EarlyStop",
            "sensor_type": "departure",
        },
        options={
            "time_window": 180,
            "refresh_interval": 300,
            "max_departures": 10,
            "max_departures_by_mode": {"train": 2, "bus": 2},
            "max_timetable_chunks": 3,
            "night_gap_by_mode": {},
        },
        unique_id="coord-early-stop",
    )
    entry.add_to_hass(hass)

    chunk_full = {
        "departures": [
            _dep("t1", "TRAIN", 5),
            _dep("t2", "TRAIN", 10),
            _dep("b1", "BUS", 6),
            _dep("b2", "BUS", 12),
        ]
    }
    chunk_extra = {"departures": [_dep("t3", "TRAIN", 70)]}
    mocked = AsyncMock(side_effect=[chunk_full, chunk_extra, chunk_extra])

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        mocked,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mocked.call_count == 1
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.data.get("departures", [])) == 4


@pytest.mark.asyncio
async def test_coordinator_continues_when_one_mode_underfilled(
    hass: HomeAssistant,
) -> None:
    """Continue fetching when buses are filled but trains are not."""
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "stop_id": "740098000",
            "name": "Continue",
            "sensor_type": "departure",
        },
        options={
            "time_window": 180,
            "refresh_interval": 300,
            "max_departures": 10,
            "max_departures_by_mode": {"train": 3, "bus": 2},
            "max_timetable_chunks": 3,
            "night_gap_by_mode": {},
        },
        unique_id="coord-continue-underfill",
    )
    entry.add_to_hass(hass)

    chunk1 = {
        "departures": [
            _dep("t1", "TRAIN", 5),
            _dep("b1", "BUS", 6),
            _dep("b2", "BUS", 12),
        ]
    }
    chunk2 = {
        "departures": [
            _dep("t2", "TRAIN", 70),
            _dep("t3", "TRAIN", 80),
        ]
    }
    mocked = AsyncMock(side_effect=[chunk1, chunk2, {"departures": []}])

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        mocked,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mocked.call_count == 2
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.data.get("departures", [])) == 5


@pytest.mark.asyncio
async def test_coordinator_stops_on_empty_chunk(hass: HomeAssistant) -> None:
    """An empty second chunk must not trigger a third API call."""
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "stop_id": "740098000",
            "name": "EmptyChunk",
            "sensor_type": "departure",
        },
        options={
            "time_window": 180,
            "refresh_interval": 300,
            "max_departures": 50,
            "max_timetable_chunks": 3,
            "night_gap_by_mode": {},
        },
        unique_id="coord-empty-chunk",
    )
    entry.add_to_hass(hass)

    mocked = AsyncMock(
        side_effect=[
            {"departures": [_dep("t1", "TRAIN", 5)]},
            {"departures": []},
            {"departures": [_dep("t2", "TRAIN", 130)]},
        ]
    )

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        mocked,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mocked.call_count == 2
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.data.get("departures", [])) == 1


@pytest.mark.asyncio
async def test_coordinator_respects_max_timetable_chunks(hass: HomeAssistant) -> None:
    """max_timetable_chunks must hard-cap API calls even when limits are unfilled."""
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "stop_id": "740098000",
            "name": "ChunkCap",
            "sensor_type": "departure",
        },
        options={
            "time_window": 360,
            "refresh_interval": 300,
            "max_departures": 50,
            "max_timetable_chunks": 2,
            "night_gap_by_mode": {},
        },
        unique_id="coord-chunk-cap",
    )
    entry.add_to_hass(hass)

    mocked = AsyncMock(
        side_effect=[
            {"departures": [_dep("t1", "TRAIN", 5)]},
            {"departures": [_dep("t2", "TRAIN", 70)]},
            {"departures": [_dep("t3", "TRAIN", 130)]},
        ]
    )

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        mocked,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mocked.call_count == 2


def test_departure_targets_satisfied_helper() -> None:
    from custom_components.trafiklab.departure_limits import departure_targets_satisfied

    items = [
        _dep("t1", "TRAIN", 5),
        _dep("t2", "TRAIN", 10),
        _dep("b1", "BUS", 6),
    ]
    assert departure_targets_satisfied(items, 10, {"train": 2, "bus": 1}) is True
    assert departure_targets_satisfied(items, 10, {"train": 3, "bus": 1}) is False
    assert departure_targets_satisfied(items, 3, {}) is True
    assert departure_targets_satisfied(items, 5, {}) is False


def test_timetable_query_times_skips_night_gap_before_service_end() -> None:
    """Evening window ending ~02:48 must not fetch during the 02:00–04:00 gap."""
    now = datetime(2026, 8, 25, 23, 48)
    times = _timetable_query_times(now, 180, night_gap=(2, 4))
    assert times == [
        None,
        datetime(2026, 8, 26, 0, 48),
        datetime(2026, 8, 26, 1, 48),
    ]


def test_timetable_query_times_resumes_morning_service_after_gap() -> None:
    """Long windows crossing the night gap resume at 04:00 instead of polling 02:00/03:00."""
    now = datetime(2026, 8, 25, 22, 0)
    anchors = [t for t in _timetable_query_times(now, 480, night_gap=(2, 4)) if t]
    assert datetime(2026, 8, 26, 2, 0) not in anchors
    assert datetime(2026, 8, 26, 3, 0) not in anchors
    assert datetime(2026, 8, 26, 4, 0) in anchors


def test_timetable_query_times_in_dead_zone_starts_at_four() -> None:
    """During the gap, the first query is 04:00 rather than the current time."""
    now = datetime(2026, 8, 26, 3, 0)
    times = _timetable_query_times(now, 120, night_gap=(2, 4))
    assert times == [datetime(2026, 8, 26, 4, 0), datetime(2026, 8, 26, 5, 0)]


def test_timetable_query_times_short_dead_zone_window_falls_back_to_now() -> None:
    """A window fully inside the gap still performs one live fetch."""
    now = datetime(2026, 8, 26, 3, 30)
    times = _timetable_query_times(now, 20, night_gap=(2, 4))
    assert times == [None]


def test_effective_night_gap_uses_intersection_for_selected_modes() -> None:
    gaps = {"train": (2, 4), "bus": (2, 4)}
    assert _effective_night_gap(["train", "bus"], gaps) == (2, 4)
    assert _effective_night_gap(["train", "metro"], gaps) is None
    assert _effective_night_gap(None, gaps) == (2, 4)


def test_normalize_night_gap_by_mode_legacy_defaults() -> None:
    gaps = _normalize_night_gap_by_mode(None)
    assert gaps["train"] == (2, 4)
    assert gaps["bus"] == (2, 4)


@pytest.mark.asyncio
async def test_coordinator_resrobot_normalizes(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "k",
            "name": "T",
            "sensor_type": "resrobot_travel_search",
            "origin_type": "stop_id",
            "origin": "o",
            "destination_type": "stop_id",
            "destination": "d",
        },
        options={"time_window": 60, "refresh_interval": 300},
        unique_id="u4",
    )
    entry.add_to_hass(hass)

    mock_resp = {"Trip": {"LegList": {"Leg": {"Origin": {"date": "2025-01-01", "time": "00:00"}}}}}
    with patch("custom_components.trafiklab.api.TrafikLabApiClient.get_resrobot_travel_search", return_value=mock_resp):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Coordinator data should have Trip as list
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(coordinator.data.get("Trip"), list)


# ---------------------------------------------------------------------------
# Platform enrichment tests (CONF_INCLUDE_PLATFORM)
# ---------------------------------------------------------------------------

_PLATFORM_TRIP = {
    "Trip": [
        {
            "LegList": {
                "Leg": [
                    {
                        "type": "JNY",
                        "number": "52",
                        "Origin": {
                            "name": "Stop A",
                            "extId": "740000001",
                            "date": "2025-01-01",
                            "time": "10:00:00",
                        },
                        "Destination": {
                            "name": "Stop B",
                            "date": "2025-01-01",
                            "time": "10:20:00",
                        },
                        "Product": {"name": "Bus 52", "num": "52"},
                        "category": "BLT",
                        "duration": "PT20M",
                    }
                ]
            }
        }
    ]
}

_TIMETABLE_DEPARTURES = {
    "departures": [
        {
            "scheduled": "2025-01-01T10:00:00",
            "realtime": "2025-01-01T10:02:30",
            "delay": 150,
            "canceled": False,
            "is_realtime": True,
            "realtime_platform": {"designation": "3"},
            "scheduled_platform": {"designation": "3"},
            "route": {"designation": "52"},
        }
    ]
}


@pytest.mark.asyncio
async def test_coordinator_resrobot_platform_enriched(hass: HomeAssistant) -> None:
    """Platform enrichment populates _realtime_platform on legs when include_platform is True."""
    # Departure entry provides the Realtime API key for platform lookup.
    # It must be added AND loaded before the resrobot entry so that the
    # domain is fully initialised and dep_entry appears in hass.data[DOMAIN].
    dep_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "realtime-key",
            "stop_id": "740098000",
            "name": "Dep Sensor",
            "sensor_type": "departure",
        },
        options={},
        unique_id="dep-for-platform",
    )
    dep_entry.add_to_hass(hass)

    # Set up only dep_entry first (this also initialises the domain).
    with patch(
        "custom_components.trafiklab.coordinator.TrafikLabCoordinator._async_update_data",
        return_value={},
    ):
        await hass.config_entries.async_setup(dep_entry.entry_id)
        await hass.async_block_till_done()

    # Add the resrobot entry AFTER the domain is loaded so HA doesn't try
    # to set it up automatically alongside the departure entry above.
    resrobot_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "resrobot-key",
            "name": "Travel",
            "sensor_type": "resrobot_travel_search",
            "origin_type": "stop_id",
            "origin": "740000001",
            "destination_type": "stop_id",
            "destination": "740000002",
        },
        options={"time_window": 60, "refresh_interval": 300, "include_platform": True},
        unique_id="resrobot-platform",
    )
    resrobot_entry.add_to_hass(hass)

    # Set up resrobot entry with mocked travel search + timetable departures.
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_resrobot_travel_search",
        side_effect=lambda *a, **kw: copy.deepcopy(_PLATFORM_TRIP),
    ), patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        return_value=_TIMETABLE_DEPARTURES,
    ):
        await hass.config_entries.async_setup(resrobot_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][resrobot_entry.entry_id]
    trips = coordinator.data.get("Trip", [])
    assert trips, "Expected at least one trip in coordinator data"
    leg = trips[0]["LegList"]["Leg"][0]
    assert leg.get("_realtime_platform") == "3", (
        f"Expected leg to have _realtime_platform='3', got {leg.get('_realtime_platform')!r}"
    )
    assert leg.get("_realtime_expected_time") == "2025-01-01 10:02:30"
    assert leg.get("_realtime_delay") == 150
    assert leg.get("_realtime_canceled") is False
    assert leg.get("_is_realtime") is True


@pytest.mark.asyncio
async def test_coordinator_resrobot_platform_no_realtime_key(hass: HomeAssistant) -> None:
    """When include_platform is True but no departure/arrival entry exists, data is returned without crash."""
    # Only a Resrobot entry — no departure/arrival entry to supply the Realtime key
    resrobot_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "resrobot-key",
            "name": "Travel",
            "sensor_type": "resrobot_travel_search",
            "origin_type": "stop_id",
            "origin": "740000001",
            "destination_type": "stop_id",
            "destination": "740000002",
        },
        options={"time_window": 60, "refresh_interval": 300, "include_platform": True},
        unique_id="resrobot-platform-no-key",
    )
    resrobot_entry.add_to_hass(hass)

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_resrobot_travel_search",
        side_effect=lambda *a, **kw: copy.deepcopy(_PLATFORM_TRIP),
    ):
        assert await hass.config_entries.async_setup(resrobot_entry.entry_id)
        await hass.async_block_till_done()

    # Data should be present and no exception raised
    coordinator = hass.data[DOMAIN][resrobot_entry.entry_id]
    assert coordinator.data is not None
    trips = coordinator.data.get("Trip", [])
    assert isinstance(trips, list)
    # No platform enrichment should have occurred (no departure entry with Realtime key)
    if trips:
        leg = trips[0]["LegList"]["Leg"][0]
        assert "_realtime_platform" not in leg
        assert "_realtime_delay" not in leg


# ---------------------------------------------------------------------------
# Error-handling tests for coordinator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coordinator_auth_error_sets_last_api_error_and_repair_issue(hass: HomeAssistant) -> None:
    """TrafikLabAuthError must set last_api_error and create a repair issue."""
    from homeassistant.helpers import issue_registry as ir
    from custom_components.trafiklab.api import TrafikLabAuthError

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "bad-key", "stop_id": "740098000", "name": "X", "sensor_type": "departure"},
        options={},
        unique_id="coord-auth-err",
    )
    entry.add_to_hass(hass)

    # Setup with a successful mock to avoid ConfigEntryNotReady during first refresh
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        return_value={"departures": []},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.last_api_error = None  # ensure clean state

    # Do exactly one controlled refresh with auth error
    # Note: async_refresh() does not raise — UpdateFailed is handled internally
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        side_effect=TrafikLabAuthError("access denied", http_status=403),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_api_error is not None
    assert coordinator.last_api_error["http_status"] == 403

    issue_reg = ir.async_get(hass)
    issue_id = f"invalid_api_key_{entry.entry_id}"
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.asyncio
async def test_coordinator_success_clears_last_api_error_and_repair_issue(hass: HomeAssistant) -> None:
    """A successful update must clear last_api_error and delete any existing repair issue."""
    from homeassistant.helpers import issue_registry as ir
    from custom_components.trafiklab.api import TrafikLabAuthError

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "key", "stop_id": "740098000", "name": "X", "sensor_type": "departure"},
        options={},
        unique_id="coord-success-clear",
    )
    entry.add_to_hass(hass)

    # Setup with success, then plant the auth error via a controlled refresh
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        return_value={"departures": []},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Note: async_refresh() does not raise — UpdateFailed is handled internally
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        side_effect=TrafikLabAuthError("access denied", http_status=403),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # Confirm issue was created
    issue_reg = ir.async_get(hass)
    issue_id = f"invalid_api_key_{entry.entry_id}"
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    # Now succeed — issue and error should be cleared
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        return_value={"departures": []},
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_api_error is None
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_coordinator_quota_error_sets_last_api_error_no_repair_issue(hass: HomeAssistant) -> None:
    """TrafikLabQuotaError must set last_api_error but must NOT create a repair issue."""
    from homeassistant.helpers import issue_registry as ir
    from custom_components.trafiklab.api import TrafikLabQuotaError

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "key", "stop_id": "740098000", "name": "X", "sensor_type": "departure"},
        options={},
        unique_id="coord-quota-err",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        return_value={"departures": []},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.last_api_error = None  # ensure clean state

    # Note: async_refresh() does not raise — UpdateFailed is handled internally
    with patch(
        "custom_components.trafiklab.api.TrafikLabApiClient.get_departures",
        side_effect=TrafikLabQuotaError("quota exceeded", http_status=429),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_api_error is not None
    assert coordinator.last_api_error["http_status"] == 429

    # No repair issue for quota errors
    issue_reg = ir.async_get(hass)
    issue_id = f"invalid_api_key_{entry.entry_id}"
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

