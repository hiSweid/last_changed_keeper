# Last Changed Keeper

[![Validate](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml/badge.svg)](https://github.com/hiSweid/last_changed_keeper/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
![Version](https://img.shields.io/github/v/release/hiSweid/last_changed_keeper?label=version)
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

Backdating only happens if the value is unchanged: the recorder walk looks
for the contiguous run of the entity's *current* (post-boot) value and stops
as soon as it hits a different one. So if a door unlocks *during* the
restart, its `last_changed` is **not** backdated to before the restart — it
keeps the time the new "unlocked" state was actually detected, exactly as if
this integration weren't installed. Only an entity whose value genuinely
didn't change across the restart gets its old timestamp restored.

Two further mechanisms run for the lifetime of the integration, not just at
boot (see [Use cases](#use-cases) below for why):

- **Runtime re-registration.** If a watched entity fully disappears and
  reappears later (its owning config entry reloads, a Zigbee/Z-Wave device
  rejoins, ...), it gets the same "now" reset a restart causes. A persistent
  listener catches exactly that transition and re-patches just that one
  entity, respecting the same grace window and retry delays as the boot pass
  — without re-running the full bulk query.
- **Incremental store.** Every genuine value change of a watched entity is
  merged (debounced ~8 s, coalesced up to 30 s under continuous chatter) into
  the same store used for the periodic/shutdown snapshot. This keeps that
  store close to real-time instead of only updating it every
  `snapshot_interval` seconds or at shutdown, and is checked ahead of an
  otherwise-definitive recorder result when it holds a newer, still-usable
  timestamp (e.g. recorder commit lag, or a change between two periodic
  snapshots). Only one entry per entity is kept, so this does not grow
  unbounded.

### last_changed vs. last_triggered

`automation.*`/`script.*` entities have the same "reset to now" problem for
their `last_triggered` attribute. This is a **separate patch path** from the
one above: `last_triggered` is an attribute, not the entity's state value, so
it needs its own recorder read (the newest historical row with a
`last_triggered` attribute) and its own apply mechanism, instead of the
value-run logic used for `last_changed`. It normally isn't needed —
automation/script entities restore `last_triggered` themselves via Home
Assistant's own restore-state mechanism — this is a best-effort correction
for when that didn't happen (a crash, a purged restore-state cache, a long
outage). Controlled by the **restore automation/script `last_triggered`**
toggle (default: on).

## Installation (HACS)

[![Open in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hiSweid&repository=last_changed_keeper&category=integration)

1. Click the badge above (or HACS → Integrations → ⋮ → *Custom repositories* →
   this repo, category *Integration*).
2. Install, restart Home Assistant.
3. *Settings → Devices & Services → Add Integration* → "Last Changed Keeper".

## Configuration

Pick **domains**, single **entities**, **labels** and/or **areas** (labels/
areas cascade through devices, same as HA's built-in label/area target
selectors), an optional **exclude** list, the **grace** window, an optional
**periodic snapshot interval** (in addition to the one written on clean
shutdown — hedges against crashes/power loss), an optional **restore
`last_updated`** toggle, a **restore automation/script `last_triggered`**
toggle (default: on — only matters if `automation`/`script` entities are
part of the selection), and the **retry delays**. Change it anytime via
*Configure*/*Reconfigure*.

## Services & events

- `last_changed_keeper.restore_now` — runs a pass on demand and optionally
  returns a response (`patched`/`last_run`).
- `last_changed_keeper.verify` — diagnostic only, never patches anything.
  Compares the live `last_changed` of every currently watched entity against
  the value derived from the recorder/store and returns any mismatches
  (`entity_id`, `live_last_changed`, `expected_last_changed`,
  `diff_seconds`). See [Troubleshooting](#troubleshooting).
- Event `last_changed_keeper_restored` fires once a pass settles (`final:
  true` in the event data) — useful for automations that would otherwise
  race the restore pass right after boot.

## Use cases

- **"Off since when?" stays trustworthy after every restart.** A light,
  switch, or climate entity that has been off/idle for weeks keeps showing
  that on the dashboard instead of "a few seconds ago" right after a Home
  Assistant update.
- **Automations that reason about "unused for N days"** (e.g. flag a device
  that hasn't changed state in a week) can rely on `last_changed` without
  every restart quietly resetting the clock — wait for
  `last_changed_keeper_restored` to avoid racing the pass right after boot.
- **History dashboards and "time in state" statistics** stay accurate across
  restarts and, since v0.7.0, across a monitored entity's own runtime
  hiccups (a Zigbee device rejoining, a config entry reloading) as well.
- **"Did this automation actually run today?"** — automations/scripts keep a
  trustworthy `last_triggered` across restarts, not just while HA stays up.

## How data is refreshed

There is no polling: everything is event- and query-driven. A **bulk**
recorder query runs once at boot for every candidate; late-recovering
entities are caught by a **state-change listener** plus a few **delayed
retries** (default +30/90/180 s). After boot, two lightweight listeners keep
running for the entry's lifetime: one **re-patches an entity that gets fully
re-registered at runtime** (see [How it works](#how-it-works)), and one
**debounces genuine value changes into the snapshot store** so it stays
close to real-time. None of this polls device state — it only reacts to
state-changed events Home Assistant already fires.

## Examples

Wait for the restore pass before evaluating "how long has this been off":

```yaml
automation:
  - alias: "Notify about long-idle heater"
    trigger:
      - trigger: event
        event_type: last_changed_keeper_restored
        event_data:
          final: true
    condition:
      - condition: template
        value_template: >-
          {{ (now() - states.switch.heater.last_changed).days > 3 }}
    action:
      - action: notify.mobile_app
        data:
          message: "Heater has been off for over 3 days."
```

Run a diagnostic check from Developer Tools → Actions and inspect the
response:

```yaml
action: last_changed_keeper.verify
data: {}
```

## Troubleshooting

- **"The last_changed value looks wrong for one of my entities."** Call
  `last_changed_keeper.verify` (with *Response data* enabled in the UI, or
  `return_response: true` from a script) and check the `mismatches` list for
  that `entity_id` — it shows the live value, the value the integration
  would derive, and the difference in seconds, without changing anything.
- **An entity never gets patched.** Check it's actually included in your
  domain/entity/label/area selection and not caught by `exclude`; the
  config/options flow shows a live count of matched entities. Also check the
  entity wasn't already outside the `grace` window at boot (real usage since
  boot is intentionally never overwritten).
- **A repair issue "incompatible HA version" appears.** The internal state
  cache structure changed in a way this version of the integration doesn't
  recognise; `last_changed` is still set, but the frontend may briefly show
  the old value. Check for an integration update or open an issue.
- **Nothing happens at all.** Confirm the [`recorder`](https://www.home-assistant.io/integrations/recorder/)
  integration is enabled (see limitations below) and check the Home
  Assistant log for `last_changed_keeper` entries.

## Known limitations

- Requires the [`recorder`](https://www.home-assistant.io/integrations/recorder/) integration — nothing to install, it ships with Home Assistant and is on by default unless you've explicitly removed it; setup fails without it.
- A brand-new entity (never seen by this integration before) is only picked
  up on the *next* restart, not retroactively. An entity re-registering
  after being seen at least once (config entry reload, device rejoin) is
  covered at runtime since v0.7.0 — see [How it works](#how-it-works).
- If a value changed while Home Assistant was off, the timestamp restored at
  boot reflects the last known change *before* shutdown, not the (unknown)
  real change time during the outage.
- The bulk recorder lookback is 30 days; entities untouched for longer than
  that rely on the snapshot/incremental store or a deeper per-entity query.
- The incremental store write is debounced (coalesced up to
  `INCREMENTAL_MAX_WAIT_SECONDS` = 30 s under continuous chatter); a crash
  within that window can lose at most that much of the very latest change
  for a given entity — the periodic/shutdown snapshot remains the backstop.
- The `last_triggered` patch is a best-effort attribute correction on the
  live state; automation/script's own restore mechanism is authoritative
  whenever it succeeds, and a later genuine trigger simply overwrites the
  patched value, as expected.

## Quality scale

This integration targets the [Home Assistant quality
scale](https://www.home-assistant.io/docs/quality_scale/) **gold** tier.
Custom integrations don't get a per-rule `quality_scale.yaml` like core
integrations do, so here's the honest breakdown: diagnostics, repair issues,
a reconfigure flow, translated exceptions, and icon translations are all
implemented; the entity/device-oriented gold rules (`devices`,
`entity-category`, `entity-translations`, `discovery`, `stale-devices`, ...)
don't apply — this integration exposes no entities or devices of its own,
it's a pure background service acting on *other* entities.

## Notes

Sets `last_changed` directly on the state object (no official API exists for
historical values); guarded defensively — on incompatibility it raises a repair
issue instead of crashing. Read-only towards the recorder.

## License

MIT — see [LICENSE](LICENSE).
