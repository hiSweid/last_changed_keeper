# Last Changed Keeper

[![Validate](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml/badge.svg)](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.5.6-blue.svg)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.11%2B-blue.svg)

Keeps the **real "last changed"** time of your entities after a Home Assistant
restart. Normally `last_changed` jumps to the restart time ("2 seconds ago"
instead of "17 minutes ago"); this integration restores the true time from the
recorder — directly on the entity, no extra sensors.

## Screenshots

<p align="center">
  <img src="docs/entity-last-changed.png" alt="Entity showing the real last changed" width="380">
  <img src="docs/config-options.png" alt="Configuration dialog" width="380">
</p>

A light on for over a week still shows *Letzte Woche* (last week) after a
restart, and the config dialog where you pick what to keep. *(German UI.)*

## How it works

On startup it reads the real start of each entity's current value from the
recorder (snapshot fallback for purged entities) and writes it back, skipping
restart artifacts (`unavailable`/`unknown`). Late-booting Zigbee/Z-Wave devices
are caught by a state listener and re-runs at +30/90/180 s. An entity is only
touched while it's "fresh" (grace, default 1800 s), so real usage isn't
overwritten. Cost is near zero: a short burst at boot, then idle.

## Installation (HACS)

1. HACS → Integrations → ⋮ → *Custom repositories* → this repo, category
   *Integration*.
2. Install, restart Home Assistant.
3. *Settings → Devices & Services → Add Integration* → "Last Changed Keeper".

## Configuration

Pick **domains** and/or single **entities**, an optional **exclude** list, the
**grace** window, an optional **restore `last_updated`** toggle, and the
**retry delays**. Change it anytime via *Configure*/*Reconfigure*. Service
`last_changed_keeper.restore_now` runs a pass on demand.

## Notes

Sets `last_changed` directly on the state object (no official API exists for
historical values); guarded defensively — on incompatibility it raises a repair
issue instead of crashing. Read-only towards the recorder.

## License

MIT — see [LICENSE](LICENSE).
