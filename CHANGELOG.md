# Changelog — Last Changed Keeper

All notable changes. Loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.0] — 2026-07-14
### Added
- **Label and area targeting.** Selecting a label or area now cascades
  through devices the same way HA's built-in label/area target selectors
  do (a label/area on a device or area applies to every entity in/on it),
  in addition to the existing domain/entity selection.
- **Periodic snapshot.** Optional `snapshot_interval` (default 6h, 0 =
  shutdown-only) writes the snapshot on a timer in addition to on clean
  shutdown — hedges against crashes/power loss where
  `EVENT_HOMEASSISTANT_STOP` never fires.
- **`last_changed_keeper_restored` event**, fired once a restore pass
  settles (`final: true` when nothing is pending anymore). Lets
  automations that depend on `last_changed` (e.g. "unused for N days")
  wait for the pass instead of racing it right after boot.
- ruff added to CI (`select = ["E","F","W","I","UP","B","SIM","RUF","BLE"]`).
### Tests
- Added `tests/test_resolve_targets.py` (label/area cascading through
  devices and areas), `tests/test_restored_event.py`, and
  `tests/test_periodic_snapshot.py`.

## [0.5.10] — 2026-07-13
### Changed
- **`restore_now` now supports a response** (`supports_response: optional`):
  returns `{"patched": N, "last_run": {...}}` when called with
  `return_response: true`. Failures during the service-triggered pass now
  raise `HomeAssistantError` (translated) instead of being silently
  swallowed; calling the service with no loaded entry raises
  `ServiceValidationError` instead of silently doing nothing.
- The service is now registered in `async_setup()` (once, independent of
  any config entry) instead of `async_setup_entry()`/`async_unload_entry()`.
  It survives entry reloads/failures instead of a brief "service not
  found" window on every options change, and calling it with the entry
  unloaded now gives a clear error rather than a no-op or a raw "service
  not found".
- Migrated the job storage from `hass.data[DOMAIN]` to `entry.runtime_data`
  (typed as `ConfigEntry[_RestoreJob]`) — the current HA convention; no
  behavior change, but diagnostics/service code no longer needs defensive
  `getattr`/`dict.get(None)` access.
- The `retry_delays` free-text field is now validated in the config/
  reconfigure/options flow (comma-separated whole seconds, 1–3600) instead
  of silently falling back to the default on bad input with no feedback.
### Tests
- Added `tests/test_init.py` (setup registers the service and
  `entry.runtime_data`; unload clears `runtime_data` but keeps the service;
  `ServiceValidationError` with no loaded entry; response support;
  `HomeAssistantError` wrapping on failure) and retry-delays validation
  tests in `test_config_flow.py`.

## [0.5.9] — 2026-07-13
### Fixed
- **Snapshot could stamp the wrong value's timestamp.** The snapshot written
  at shutdown stored only a timestamp, not the state value it belonged to.
  If an entity's value genuinely changed while Home Assistant was down (or
  crashed instead of shutting down cleanly), the fallback chain could apply
  the *previous* value's last-changed time to the *new* value. The snapshot
  now stores the state value alongside the timestamp and is only used when
  the entity still holds that exact value; the old timestamp-only format is
  discarded gracefully. Related: a bounded recorder result that fails the
  freshness margin (the value provably *just* changed) now returns `None`
  immediately instead of falling through to a stale snapshot or deep query.
- **`restore_now` could permanently orphan pending entities.** Calling the
  service while the boot-time retry pass was still active (listener +
  30/90/180s timers waiting on late-booting devices) reset that machinery,
  silently abandoning every entity still pending for the rest of the grace
  window. The service now runs an in-place pass over the currently pending
  entities instead of tearing down and resetting the job state.
- **Deep per-entity fallback could return a wildly-too-recent timestamp** on
  attribute-noisy domains (`climate`, `humidifier`: frequent
  attribute-only updates with the same state value can fill the entire
  100-row query window). That best-effort result is now discarded when the
  row window was exhausted by the row-count limit rather than by reaching
  an actual older value.
- **Live state could go stale across the bulk-query await.** Between
  building the candidate list and awaiting the recorder bulk query,
  seconds can pass on a busy boot. Each candidate's state is now
  re-validated (unavailable / genuinely-just-changed) right before
  resolving and patching it.
- Config/reconfigure/options flow: reconfiguring after ever having used the
  options dialog was a silent no-op — the options flow writes the full form
  into `entry.options`, which always wins the `{**data, **options}` merge
  every runtime read uses. Reconfigure now clears `entry.options` on save.
- A domain saved earlier but with zero current live states (its integration
  temporarily disabled/broken) is now kept selectable in the dropdown
  instead of failing validation or being silently dropped on the next save.
- Missing `reconfigure_successful` translation: every successful
  reconfigure showed the raw, untranslated abort key. Added in all 8
  languages.
### Changed
- Target-resolution logic (`domains ∪ entities − exclude`) was duplicated
  between the restore job and the config flow's live-count/empty check;
  extracted into a single shared `resolve_targets()`.
- Diagnostics now dump the full merged config instead of a hand-picked
  subset that omitted `exclude`, `retry_delays` and `restore_last_updated`
  — exactly the settings needed to debug "why wasn't entity X restored".
- `manifest.json`: dropped the redundant `after_dependencies: [recorder]`
  (already covered by `dependencies`).
- `services.yaml`: removed a description sentence that had drifted from
  (and was never shown instead of) the translated service description.
- CI: pinned test dependencies via `requirements_test.txt` instead of
  always installing latest; restricted the `push` trigger to `main` so PRs
  no longer run every job twice.
### Docs
- README: fixed the stale version badge (now a dynamic GitHub-release
  badge) and added a "Known limitations" section (recorder required,
  post-boot entities, changes made while HA was off, 30-day bulk lookback).
- Fixed a broken reference-style Markdown link in this changelog.
- Removed the deprecated `render_readme` key from `hacs.json`.
### Tests
- Added `tests/test_resolve.py` (snapshot state-value matching, the
  bounded-but-not-ok short-circuit, the exhausted-history-window guard),
  `tests/test_restore_job_lifecycle.py` (the `restore_now` /
  active-retry-machinery regression), and two more `test_config_flow.py`
  cases (reconfigure-after-options regression, domain-dropdown-union).

## [0.5.8] — 2026-07-12
### Fixed
- Config/options/reconfigure flow: selecting a domain and then excluding
  every one of its entities via the exclude list is now correctly rejected
  as an empty selection, instead of silently creating an entry with zero
  effective targets.
### Changed
- `_resolve`: an unbounded bulk-query result is now kept as a cheap
  best-effort fallback candidate if the deep per-entity query is
  inconclusive or errors, instead of being discarded outright.
### Tests
- Added `tests/test_config_flow.py` (config, reconfigure and options flow,
  including the exclude-empties-domain regression) and
  `tests/test_apply_and_bulk.py` (`_apply_last_changed` incl. cache and
  degraded-mode paths).

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
