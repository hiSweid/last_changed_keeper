# Last Changed Keeper

[![Validate](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml/badge.svg)](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.5.5-blue.svg)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.4%2B-blue.svg)

Home Assistant integration that restores the **real "last changed"** time
(`last_changed`) of selected entities after a restart — directly on the entity,
without extra sensors.

## Problem

After a Home Assistant restart, the native `last_changed` of many entities shows
the restart time instead of the real last usage ("2 seconds ago" instead of
"17 minutes ago"). Reasons: integrations write a fresh state on startup, and
devices (Zigbee/Z-Wave) go through `unavailable → on/off`.

## Solution

On startup the integration determines when the current value really began and
writes that time back into the live state object. Sources, in order:

1. **Recorder bulk query** — a single query for all entities (fast).
2. **Recorder per-entity query** (deeper) as a fallback for ambiguous cases.
3. **Snapshot store** — written on the last shutdown; covers entities the
   recorder no longer has (recorder exclude, short `purge`).

Restart artifacts (`unavailable`/`unknown`) are skipped.

To also catch late-booting devices, this runs in several stages:

1. **Immediately** on `homeassistant_started` for already-available entities.
2. **State listener** for devices that return from `unavailable` afterwards.
3. **Delayed re-runs** (+30 s, +90 s, +180 s) for multi-step boot sequences
   (`unavailable → off → on`).

An entity is only touched while its `last_changed` is younger than the grace
window (default 1800 s) — so real usage shortly after boot is never overwritten.

## Performance

Practically zero in normal operation: after startup the listeners and timers are
torn down again, there are no sensors, no polling, no recorder writes. The only
cost is a short burst at startup (one bulk query + a few per-entity fallbacks)
and a single snapshot write on shutdown.

## Installation (HACS)

1. HACS → Integrations → ⋮ → *Custom repositories* → add this repo URL,
   category *Integration*.
2. Install "Last Changed Keeper", restart Home Assistant.
3. *Settings → Devices & Services → Add Integration* → "Last Changed Keeper".

## Configuration (GUI)

- **Domains** – all entities of these domains (default: light, switch, cover,
  fan, climate, lock, media_player, input_boolean, humidifier, vacuum).
- **Additional individual entities**.
- **Excluded entities** – drop individual entities from the selected domains.
- **Grace** – maximum age of `last_changed` above which it is no longer patched.
- **Also restore last_updated** – optionally restore `last_updated` too.
- **Retry delays** – comma-separated seconds for the delayed re-runs.

Everything can be changed later via *Configure* or *Reconfigure*.

## Service

`last_changed_keeper.restore_now` – runs a restore pass immediately (e.g. after
adding new entities).

## Compatibility & notes

- Tested with Home Assistant 2026.6.
- The integration sets `last_changed` directly on the state object and
  invalidates its cache (internal HA structure; there is no official API for
  historical `last_changed`). All accesses are defensively guarded: if the cache
  access fails, nothing is overwritten and a repair issue is raised — no crash.
- Read-only towards the recorder; nothing is changed there persistently.

## Branding

App icon and logo live under
`custom_components/last_changed_keeper/brand/`. To make them show up everywhere
in Home Assistant, a separate pull request to
[home-assistant/brands](https://github.com/home-assistant/brands) is required.

## License

MIT — see [LICENSE](LICENSE).
