# Trafiklab for Home Assistant (personal fork)

A Home Assistant custom integration for Swedish public transport, built on the **Trafiklab Realtime API** and **Resrobot API**. It shows live departures/arrivals for a stop and end-to-end journey planning.

> [!IMPORTANT]
> **This is a personal fork, not the original project.**
>
> It is forked from [MrSjodin/HomeAssistant_Trafiklab_Integration](https://github.com/MrSjodin/HomeAssistant_Trafiklab_Integration) — all credit for the integration goes to **Mattias Sjödin ([@MrSjodin](https://github.com/MrSjodin))**. If you're looking for the maintained, general-purpose integration, use the original.
>
> This fork exists for my own setup and is **not actively maintained**. I merge in upstream releases when I find the time and when they don't break my use case, but there are no guarantees, no roadmap, and no support. Feel free to use it, but expect to fend for yourself.
>
> It is modified to work with my own departures card, [ha-trafiklab-departures-card](https://github.com/vitords/ha-trafiklab-departures-card). **I can't guarantee compatibility with the dashboard/Lovelace cards by the original author**, since the sensor attributes differ in places.

This integration is community-developed and is not affiliated with Trafiklab/Samtrafiken.

## What's different in this fork

On top of upstream, the Departures/Arrivals sensors have:

- **Departure limits per transport mode** (bus, metro, train, tram, boat) and **per direction** (e.g. `söder:8, norr:8`), instead of a single result count
- **Multi-chunk time windows**: windows longer than 60 minutes are fetched in chunks, capped by a configurable *max API chunks per refresh* to protect your API quota
- **Night-gap handling**: configurable per-mode "dead zone" hours so a late-night query can skip ahead to the next morning's service
- **Request statistics**: counts of real HTTP requests per entry (today / total / per endpoint), exposed as a sensor and in diagnostics, to keep an eye on your quota
- **Slimmer `upcoming` attributes** (optional fields such as notices, agency and route name are only included when present)
- A [Fully Kiosk automation example](examples/automations/fully_kiosk_trafiklab_refresh.yaml) for refreshing faster only while a dashboard is visible

Everything else (Travel Search sensors, services, filters) is as upstream. Upstream's *Maximum Number of Results* setting applies to Travel Search sensors only; Departures/Arrivals use the limits above.

## Features

- Live departures and arrivals for any Trafiklab-covered stop in Sweden
- Resrobot travel search between stops, coordinates, stop names, HA zones or person entities
- Per-sensor filters: line, destination, transport mode, time window, maximum trip duration
- Services: `stop_lookup`, `travel_search`, `trip_details`, `update_now`
- UI setup (config flow), English and Swedish translations

## Installation

### HACS (custom repository)

1. In HACS, open the ⋮ menu → **Custom repositories**
2. Add `https://github.com/vitords/ha-trafiklab` with type **Integration**
3. Download **Trafiklab** and restart Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=vitords&repository=ha-trafiklab&category=integration)

If you have the original integration installed, remove it first — both use the `trafiklab` domain, so they can't coexist.

### Manual

Copy `custom_components/trafiklab` into your Home Assistant `custom_components` directory and restart.

### Prerequisites

- Home Assistant 2024.8.0 or newer
- A free API key from [Trafiklab](https://www.trafiklab.se/) for the Realtime API, and a separate one for Resrobot if you want Travel Search. Mind the default API quota.
- Travel Search sensors cross-check delays against the Realtime API, so set up a Departures/Arrivals sensor first (its key is reused automatically).

## Setup

Add it via **Settings → Devices & Services → Add Integration → Trafiklab**, pick Departures, Arrivals or Travel Search, and fill in the stop and filters. Stop IDs can be found with the `trafiklab.stop_lookup` service.

## Documentation

These guides come from upstream, and may not describe the fork-specific options above:

- [Installation & configuration](docs/setup.md): stop ID lookup, setup walkthrough, filters, update-condition templates
- [Sensor & service reference](docs/reference.md): attribute schemas and service examples
- [Automation & dashboard examples](docs/examples.md)

## APIs used

- [Trafiklab Realtime APIs](https://www.trafiklab.se/api/our-apis/trafiklab-realtime-apis/) (Timetables, Stop Lookup)
- [Resrobot v2.1](https://www.trafiklab.se/api/our-apis/resrobot-v21/) (Travel Search, Stop Lookup)

Operator coverage depends on Trafiklab; see their [Timetables page](https://www.trafiklab.se/sv/api/our-apis/trafiklab-realtime-apis/timetables/) for the current list.

## License

MIT — see [LICENSE](LICENSE). Original copyright © Mattias Sjödin.

## Acknowledgments

- **[Mattias Sjödin (@MrSjodin)](https://github.com/MrSjodin)** for the original integration this is built on
- [Trafiklab](https://www.trafiklab.se/) for the public transport APIs
- The [HASL developers](https://github.com/hasl-sensor/), whose integration inspired the original
