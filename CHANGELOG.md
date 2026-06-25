# Changelog — Last Changed Keeper

All notable changes. Loosely based on [Keep a Changelog].

## [0.5.7] — 2026-06-25
### Fixed
- CI: pytest could not import `custom_components` (repo root not on `sys.path`).
  Added `pyproject.toml` with `pythonpath = ["."]`.

## [0.5.6] — 2026-06-24
### Changed
- Options flow modernized to the new pattern (no `config_entry` passed to the
  constructor; uses the built-in `self.config_entry`) — avoids the API removed in
  HA 2025.12.
- Config flow now uses `ConfigFlowResult` instead of the deprecated `FlowResult`.
- Minimum Home Assistant version raised to 2024.11.

## [0.5.5] — 2026-06-24
### Added
- Brand icons (logo + app icon, @1x/@2x) under
  `custom_components/last_changed_keeper/brand/`.
- GitHub Actions workflow `validate.yml` (hassfest, HACS validation, pytest).
- README status badges.
### Tests
- Expanded pytest coverage: `test_parse_delays.py` (9 cases) and additional
  `_real_last_changed` edge cases.

## [0.5.4] — 2026-06-23
### Added
- Polish translation (pl). Eight languages total.

## [0.5.3] — 2026-06-23
### Added
- Portuguese translation (pt).

## [0.5.2] — 2026-06-23
### Added
- Italian translation (it).

## [0.5.1] — 2026-06-23
### Added
- Configurable retry delays (comma-separated seconds, e.g. "30, 90, 180").
  Parsed robustly, clamped to 1–3600 s, falling back to the defaults on invalid
  input. In all languages.

## [0.5.0] — 2026-06-23
### Added
- "Also restore last_updated" option (toggle, default off): optionally also sets
  `last_updated` (+ timestamp slot) to the real time so the "last updated"
  display is correct too. Default off → existing behavior unchanged.

## [0.4.7] — 2026-06-23
### Fixed
- The "incompatible HA version" repair issue now auto-resolves as soon as the
  cache patch works again in a run (e.g. after an HA update). Previously a stale
  issue stayed forever.

## [0.4.6] — 2026-06-23
### Added
- Translations for French (fr), Spanish (es) and Dutch (nl).

## [0.4.5] — 2026-06-23
### Changed
- `strings.json` is now the English base (convention); German comes from
  `translations/de.json`. Users in other languages now get an English fallback
  instead of German text.

## [0.4.4] — 2026-06-23
### Added
- Exclude list: individual entities can be excluded from the selected domains
  (config, reconfigure and options flow). Affects restore targets and snapshot;
  default empty → no behavior change for existing setups.

## [0.4.3] — 2026-06-23
### Fixed
- The English translation (`translations/en.json`) contained German text →
  English users saw German. Now properly translated (same key structure).

## [0.4.2] — 2026-06-23
### Added
- Reconfigure flow (`async_step_reconfigure`): an existing setup can be
  reconfigured without deleting and re-adding it.

## [0.4.1] — 2026-06-23
### Added
- The config/options flow shows the live count of affected entities.
### Changed
- An empty selection (neither domain nor entity) is now caught and reported with
  an error instead of allowing an ineffective setup.

## [0.4.0] — 2026-06-23
### Added
- Restore the native `last_changed` of selected entities from the recorder after
  a restart, directly on the entity (no extra sensors).
- Recorder bulk query (`get_significant_states`) with per-entity fallback.
- Snapshot store (written on shutdown, read on start) as a fallback for entities
  the recorder no longer has.
- Diagnostics download with the last run's stats.
- Repair issue if the internal state cache structure is unknown.
- `restore_now` service and pytest tests for `_real_last_changed`.
### Fixed
- Cold-start race: `hass.is_running` is already True during `starting` → ran
  before entities were loaded. Now started via `async_at_started`.
- Recorder recovery artifacts (`unavailable → off`) returned the restart time →
  now a contiguous-run scan over valid states.
