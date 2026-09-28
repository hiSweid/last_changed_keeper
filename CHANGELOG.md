# Changelog — Last Changed Keeper

All notable changes. Loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9.11] — 2026-09-12
### Added
- **`button.restore_now`.** Lets users trigger a restore pass from the
  entity/device page for debugging, without an action call or building a
  dashboard button.

## [0.9.10] — 2026-09-03
### Fixed
- **Reconfigure no longer reloads the entry twice.** The reconfigure step
  called `async_update_reload_and_abort()`, which schedules its own reload
  on top of the config-entry update listener already registered in
  `async_setup_entry` — the listener reloads on *any* data/options change,
  so every reconfigure save reloaded the integration twice back-to-back
  (and, per Home Assistant's own docs, could race). Switched to
  `async_update_and_abort()`, which updates the entry and lets the existing
  listener drive the single reload. Combining a config-entry listener with
  a config-flow reloading method has been deprecated since HA Core 2026.6
  and turns into a hard error from 2026.12 onward.

## [0.9.15] — 2026-09-14
### Fixed
- **An entity torn down and recreated with an unchanged value, repeatedly,
  could get a different "true" `last_changed` answer depending on when
  you asked — sometimes falling back to no answer at all.** The bulk
  query's per-entity row cap (`BULK_PER_ENTITY_LIMIT`) counted every row
  that passed its "genuine value change" filter, including a same-value
  recreation row written right after a removal (v0.9.14 stopped the
  removal row itself from being trusted as a boundary, but the
  recreation row immediately following it still consumed a cap slot).
  `_real_last_changed`'s walk already handled a same-value row correctly
  — it just extends the run backward — so this was never a walk-logic
  bug; it was purely that the cap could be spent entirely on
  non-informative rows before the walk ever saw them.

  Field-diagnosed on the same installation: `binary_sensor.echo_dot_bedroom`
  and dozens of entities across unrelated integrations (Echo devices,
  UniFi trackers, `magic_areas` aggregates) get torn down and recreated
  in mass bursts every few hours, driven by Alexa Devices' and UniFi
  Network's own documented reliability issues (session refresh failures,
  websocket disconnects) cascading through `magic_areas`'s area
  aggregation. `_resolve()` computed two different answers for the same,
  apparently-unchanged entity at two points in a single 38-hour session
  with no restart in between — an inconsistency that can only get worse
  the longer an install goes without a restart, since every reload cycle
  eats another cap slot.

  Fixed by deduplicating consecutive same-value rows in `_bulk_query`
  before applying the cap (a `LAG()`-based "only count genuine
  transitions" pass, ahead of the existing `row_number()` ranking) —
  the cap now always represents up to `BULK_PER_ENTITY_LIMIT` genuine
  transitions, regardless of how many teardown/recreate cycles happened
  in between. This is deliberately general-purpose: it isn't specific to
  Alexa Devices or UniFi Network, and helps against any integration's
  reload behavior having the same effect. The equivalent gap in the
  deep-query fallback path (`_resolve()` step 3, via HA core's own
  `get_last_state_changes` — which applies no filtering at all before
  its own cap, not even for attribute-only or unavailable rows) is not
  addressed here: fixing it would mean replacing that call with a custom
  query, which is a larger undertaking than this release scopes to, and
  there's no field evidence yet that path has caused a wrong answer.

## [0.9.14] — 2026-09-12
### Fixed
- **An entity that survived several restarts without a genuine value
  change could get permanently, confidently patched to an intermediate
  restart's own artifact instead of its true origin.** `Entity.
  async_remove()` writes a `None`-state row to the recorder on every
  graceful entity teardown — an ordinary graceful HA restart as well as a
  config-entry reload — and `_real_last_changed` let such a row bound a
  run just like a genuine differing value. 0.9.12's `bounded_by_removal`
  tried to avoid trusting a *too-recent* instance of this, but measured
  "too recent" against the entity's own current `live.last_changed` —
  which is itself whatever the last resolve wrote, so the guard only ever
  protected the single most-recently-written removal row. Any older one
  an entity accumulated over multiple restarts got trusted as if it were
  a real transition, `_apply()`'d, and the entity marked `_confirmed` —
  after which, per this integration's own `_confirmed`-means-never-
  revisited design, nothing ever re-checked it again.

  Field-diagnosed on the same installation: a cluster of 42 entities
  spanning many unrelated integrations (Echo devices, UniFi switches,
  `magic_areas` aggregates, device trackers) were all patched to a stale
  restart timestamp from days earlier during one boot pass, and never
  touched again — a fresh `verify` call hours later independently
  recomputed the true, still-earlier origin for the same entities. The
  same shape recurred from an even earlier restart for a different pair
  of entities, confirming it as a general, recurring bug rather than a
  one-off.

  Fixed by treating a removal row exactly like `unavailable`/`unknown`:
  transparent to `_real_last_changed`'s walk, regardless of age — it
  neither extends a run nor bounds one, so the walk simply continues past
  it looking for a row where the value genuinely differs. This removes
  `bounded_by_removal` and its age heuristic entirely rather than
  patching it further; the one thing that heuristic partly preserved —
  not trusting history from before an entity_id was reused for a
  genuinely different device — isn't a scenario this integration needs to
  guard against. `_bulk_query` also stops fetching removal rows in the
  first place, since they can no longer bound anything: on an install
  that's been through several restarts recently, every one of those rows
  was previously wasting a slot in the per-entity row cap
  (`BULK_PER_ENTITY_LIMIT`) that could otherwise have reached a genuine
  value change further back.

## [0.9.13] — 2026-09-08
### Fixed
- **0.9.12's recorder-commit-lag protection could itself stall real
  sensor history for hours on a busy install.** `_wait_for_recorder_commit()`
  (added in 0.9.12) is called before every recorder history query this
  integration issues — one per bulk batch, one per still-unresolved
  entity's deep/`last_triggered` query — in a plain sequential loop with no
  pacing of its own. `Recorder.async_get_commit_future()` is only free when
  the write queue happens to already be empty; on a non-empty queue it
  enqueues a `SynchronizeTask`, and every `RecorderTask` defaults to
  `commit_before=True`, so it forces an *early* commit rather than letting
  the recorder batch writes over its own ~5-second commit interval. Calling
  this unconditionally before every one of potentially hundreds to
  thousands of sequential queries (worse on a "track all entities" install,
  and repeated by the periodic sweep every 5 minutes for anything still
  `_unconfirmed`) turned that efficient batched commit behaviour into one
  forced commit per query.

  Field-reported after upgrading: long blanks (same value, no updates, for
  several hours) in sensor value graphs, no related log messages, and the
  problem stopped when the integration was disabled. The install's log
  showed the "recorder commit still pending after 10s" DEBUG line
  repeatedly — the wait wasn't just adding overhead, it was a
  self-reinforcing feedback loop: each stalled forced-commit attempt added
  more load to an already-overloaded recorder, making the next one more
  likely to stall too, at wall-clock scale enough to stop real sensor
  history from being recorded at all.

  Fixed by coalescing: `_wait_for_recorder_commit()` now only genuinely
  checks the recorder at most once per
  `RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS` (new constant, default 2s,
  tracked via `self._last_commit_sync_monotonic`) — a call within that
  window returns immediately without touching the recorder at all. This
  caps how often this integration can force an early commit, independent
  of how many queries a given pass issues, while still catching commit lag
  on the same timescale as 0.9.12's original field evidence (a
  3-second-old transition).

## [0.9.12] — 2026-09-03
### Fixed
- **A recorder history query could miss a state change that had already
  happened — visible via `hass.states.get()` — but hadn't been committed
  to the database yet, and `_resolve()`'s "bounded" result is trusted
  unconditionally the instant it finds any older, differently-valued row,
  with no protection against the query simply having run ahead of the
  recorder's own write queue.** Recorder writes are queued and committed
  asynchronously rather than synchronously with the in-memory state
  change (ordinary commit lag), so a query issued immediately after a
  genuine transition can silently see a stale, older boundary instead —
  wrong, but indistinguishable from a correct bounded result at the call
  site. An entity hit by this at boot gets `_apply()`'d once, marked
  `_confirmed`, and — since only `_unconfirmed` entities are ever
  revisited by the periodic sweep — sits wrong until the next full
  restart, with nothing to retry it.

  Field-diagnosed on the same installation after 0.9.9 (which fixed the
  two prior bugs cleanly — 685 mismatches down to 92 at first post-deploy
  `verify`): a `verify` dump six hours later, no restart in between, had
  crept back up to 117. Cross-referencing that dump against the install's
  own log found a real door sensor whose `verify`-computed
  `expected_last_changed` matched a transition from the previous evening,
  even though the door had genuinely, physically reopened and closed
  again 3 seconds before the query that produced that answer — proof the
  query ran ahead of the recorder's commit, not that the door was
  actually still in its earlier state. The same log also showed most of
  that 117 was a separate, non-bug artifact: entities being correctly,
  repeatedly re-patched throughout the run (one 73 times) that `verify`
  still reported as mismatched simply because `verify`'s own multi-minute
  scan reads each entity's live value at whatever point it reaches it,
  not atomically at report time — this fix, sharing the same query path,
  also reduces that false-positive noise.

  Adds `_wait_for_recorder_commit()`, called before every recorder
  history query this integration issues (each bulk batch, each per-entity
  deep/`last_triggered` query) — the same `Recorder.async_get_commit_
  future()` mechanism HA core's own live-history websocket API uses
  before querying, bounded by a short `RECORDER_COMMIT_WAIT_TIMEOUT_
  SECONDS` timeout (distinct from and much shorter than 0.9.9's boot-time
  `RECORDER_READY_TIMEOUT_SECONDS`, since this guards an ordinary
  sub-second-to-few-second race that can be hit many times within a
  single pass, not a one-time migration gate). Costs nothing in the
  common case: the write queue is usually already empty, and the call
  returns immediately when so.

- **A fast re-registration could permanently block `_resolve()` from ever
  falling through to the snapshot, making the entity worse off than before
  the fix above.** `Entity.async_remove()` — called on every config-entry
  reload or device rejoin, exactly the case the re-registration listener
  exists to fix — makes the recorder write a `None`-state row for the
  removal, and `_real_last_changed` already (correctly, deliberately)
  treats that row as a genuine boundary the same as any differing value.
  But `_resolve()`'s bounded-result trust doesn't distinguish *why* a
  result is bounded: a boundary that's too recent to clear the margin is
  hard-blocked (`return None`) on the assumption that a genuine value
  transition was proven, so no other source may override it. A removal
  boundary proves no such thing — it only proves the entity briefly didn't
  exist — yet a fast reload (well under `MARGIN_SECONDS`) was hitting the
  same hard block, and since the bulk history behind that block never
  changes, every later retry (`RETRY_DELAYS`, the periodic sweep)
  recomputed the identical answer forever: permanently `_unconfirmed`,
  never reaching the snapshot fallback that would otherwise have restored
  it correctly.

  Found while shipping the fix above: making history queries commit-lag-
  safe means the bulk query now reliably sees the removal row it used to
  sometimes race past, which is what turned this from a latent gap into
  two failing regression tests. `_real_last_changed` now reports *why* a
  result is bounded (`bounded_by_removal`); `_resolve` still trusts a
  too-recent genuine value transition unconditionally (that part was
  always correct), but treats a too-recent removal boundary as
  inconclusive and falls through to the snapshot/deep/best-effort sources
  below instead.

## [0.9.9] — 2026-08-30
### Fixed
- **The periodic sweep added in 0.9.8 excluded anything currently in
  `self._pending`, silently recreating the exact "abandoned after one
  failed resolve" gap 0.9.8 was meant to close — just for the boot-pending
  population instead of the general unconfirmed one.** `self._pending` is
  only ever populated during the initial boot pass and never regrows
  afterward; an entity that recovers from unavailable to a real value gets
  exactly one resolve attempt (the debounced recovery listener, or the
  fixed `RETRY_DELAYS` ladder, ~30/90/180s) and, if that one attempt fails
  — plausible on a recorder cold-start race — stayed in `self._pending`
  forever with nothing left to retry it, since the periodic sweep's `stale
  = self._unconfirmed - new_targets - self._pending` computation excluded
  it by construction.

  Field-diagnosed on the same ~3,500-entity installation, several days
  into running 0.9.8: 685 `verify` mismatches remained, and for every one
  still sitting at that boot's own raw restart timestamp, `verify`'s
  freshly computed answer exactly matched what was already sitting in the
  snapshot store — the correct value had been available the whole time,
  nothing had ever asked for it again.

  Drops the `self._pending` exclusion: `NEW_TARGET_SCAN_INTERVAL_SECONDS`
  (300s) is longer than the boot-time retry ladder's maximum delay (180s),
  so this sweep's first tick can never fire while that ladder is still
  active, and `_drain_unconfirmed_burst`'s own per-entity live-state check
  already skips anything genuinely still unavailable at zero recorder
  cost. A resolve that now succeeds for a still-`_pending` entity also
  clears its `_pending` bookkeeping (see `_resolve_candidates`), so the
  boot job's "final restored" event and listener teardown — which only
  fire once `_pending` is fully empty — are no longer permanently stuck
  behind an entity nothing was ever going to revisit.

- **`async_write_snapshot` trusted any entity merely *absent* from
  `self._unconfirmed` as ground truth, but an entity the boot pass's grace
  check silently classified as "already genuinely used since boot" was
  never added to that set at all — it received zero tracking, positive or
  negative.** That classification only holds if `_async_run_impl` itself
  runs shortly after the true HA restart; if it runs late (a slow recorder
  migration, general startup congestion — plausible right after an
  HA/integration upgrade), a still-pristine restart-artifact entity slips
  through untracked, and the very next periodic (6h) or shutdown snapshot
  write confirms that artifact into the snapshot as ground truth —
  permanently, since nothing ever re-checks an already-"confirmed" entity
  again.

  Field-diagnosed on the same installation by cross-referencing the live
  `verify` mismatches against the on-disk snapshot store directly: the
  store held single-minute clusters of 578, 220, 200, 106, 78, 64, 33 and
  23 entities each — restart-artifact shaped, not real per-entity history
  — and 272 of the 685 current mismatches matched a snapshot entry
  exactly. One cluster (78 entities) landed at the exact run in
  `last_changed_keeper.runs` where the `BULK_PER_ENTITY_LIMIT` migration
  shows up — an upgrade event, exactly the kind of moment startup
  congestion is most likely.

  Flips snapshot-write trust from a blocklist to an allowlist: a new
  `self._confirmed` set, populated only by positive evidence (a
  successful `_resolve()`+`_apply()`, or a genuine value transition
  observed by the incremental listener) via two small `_mark_confirmed`/
  `_mark_unconfirmed` helpers that keep it and `self._unconfirmed` from
  drifting out of sync, is now the *only* thing `async_write_snapshot`
  trusts. Everything else — including a grace-skipped entity that was
  never examined at all — carries forward whatever is already on disk
  instead of being freshly trusted off an unverified assumption. Does not
  retroactively clean an already-poisoned on-disk snapshot; clear
  `.storage/last_changed_keeper.snapshot` once after upgrading to let it
  rebuild through the now-safe resolve chain.

- **The boot pass could start querying the recorder before the recorder
  was genuinely ready, not just "set up."** `manifest.json`'s `recorder`
  dependency only guarantees `Recorder.async_db_ready` has resolved (DB
  connected, non-live migration done) before this integration's own setup
  runs. A *live* schema migration — needed after most HA core upgrades —
  is deliberately deferred by the recorder itself until *after* HA reports
  "started," specifically so it doesn't compete with startup CPU load —
  which is exactly the same moment this integration's own boot trigger
  (`async_at_started`) fires. Without a wait, the boot pass's recorder-heavy
  bulk/deep queries would run directly against a mid-migration recorder,
  competing for the same executor/DB the migration was deferred to avoid
  competing with — plausibly explaining the field install's abnormally
  long (~30 minute) boot pass, and giving the "why would `_async_run_impl`
  run late relative to the true restart" scenario behind the
  snapshot-poisoning fix above a concrete, recurring mechanism rather than
  a hand-waved one.

  Adds a bounded wait for `Recorder.async_recorder_ready` (an
  `asyncio.Event`, distinct from `async_db_ready`, only set once all
  migration steps genuinely finish) at the start of `async_run()` — the
  boot-only wrapper; `restore_now`'s service handler calls
  `_async_run_impl` directly and is unaffected. Bounded rather than
  unconditional because a *failed* live migration never sets the
  readiness signal at all; on timeout (`RECORDER_READY_TIMEOUT_SECONDS`,
  a generous default given large/low-power installs have been reported
  taking up to an hour for such a migration) the boot pass proceeds
  anyway rather than hanging forever.

## [0.9.8] — 2026-08-22
### Fixed
- **An entity that had a live value at boot but simply failed to resolve
  was abandoned forever — no retry ladder, no listener, nothing ever
  revisited it again.** Unlike a boot-pending entity (no live state at all,
  which gets a listener plus the `RETRY_DELAYS` retry ladder), a candidate
  that resolves during the boot pass and comes back `None` was only ever
  marked `self._unconfirmed`; nothing scheduled a further attempt. The same
  gap existed for `_drain_reregister_burst` candidates (re-registrations
  and 0.9.7's newly-discovered entities) once their own retry ladder
  exhausted. For the largely-static diagnostic/config entities this hits
  hardest — battery/info sensors, aggregate/health sensors, alarm-panel
  zones, and similar entities that rarely if ever change value again —
  nothing was ever going to trigger a retry, so a single failed resolve
  attempt meant the entity stayed wrong for the rest of the session.

  Field-diagnosed on the same ~3,600-entity installation via a `verify` run
  taken after 0.9.7 was already deployed: 675 mismatches spanning nearly
  every domain and integration in the install (Magic Areas aggregates, a
  k8s cluster monitor, an alarm panel, Z-Wave diagnostics, `alert2`,
  automations, and more) — too broad to be integration-specific. The
  tell was in the timestamps: `verify`'s freshly-computed expected values
  were themselves often just *older* restart artifacts shared by dozens of
  unrelated entities, and `verify` — called well after boot, with the
  recorder fully warmed up — resolved these entities correctly using the
  exact same `_resolve()` chain that had failed for them at boot time.
  Most likely a recorder cold-start race on a large install (bulk/deep
  queries outrunning the recorder's own startup work), though the specific
  cause doesn't change the fix: a candidate whose resolve attempt failed is
  a dead end architecturally identical to the "entity didn't exist yet at
  boot" gap 0.9.7 fixed for a different population.

  Extends the periodic sweep already added in 0.9.7
  (`_async_scan_for_new_targets`, same `NEW_TARGET_SCAN_INTERVAL_SECONDS`
  timer) with a second pass: re-attempt resolution for everything still in
  `self._unconfirmed` — the set that already reliably tracks "this
  entity's live `last_changed` is still a fake artifact" — excluding
  anything currently in `self._pending` (already being actively chased by
  the boot-pending listener/retry ladder). Unlike the discovery half of the
  sweep, this new `_drain_unconfirmed_burst` deliberately does not
  grace-filter its candidates: an artifact that's now hours or days stale
  by wall-clock time is exactly the entity this fix needs to catch, not one
  to skip. The streaming resolve/apply loop shared by both drains was
  factored out into `_resolve_candidates` to avoid duplicating it a third
  time.

## [0.9.7] — 2026-08-18
### Fixed
- **Entities whose owning integration is still mid-setup when HA "started"
  fires were invisible to every patch mechanism this integration has, for
  the entire session.** `_async_run_impl` resolves the set of targets to
  watch exactly once, at boot, from whatever entities already have a live
  state at that instant. Any entity that doesn't exist yet at that moment —
  a coordinator-based integration (cloud-polling, hub/mesh) doing its first
  data fetch before creating entities is a common cause — was excluded not
  just from that boot pass, but from the persistent re-registration and
  incremental-store listeners too, since both subscribe against that same
  one-time snapshot. The entity's `last_changed` then simply stayed at
  whatever it was when first created for the rest of the session, with
  nothing ever correcting it, self-healing only at the next full restart
  (where the same race could recur for the same reason).

  Field-diagnosed on the same ~3,500-entity installation via a fresh
  `verify` run: clustering the mismatches' live timestamps by minute showed
  three restart events, each followed a few minutes later by a second,
  larger sub-cluster of entities frozen at first-creation time — matching
  several coordinator-based integrations (an alarm/security hub, a
  smart-speaker family, a mesh-node integration enumerating many peers)
  that took anywhere from a couple of minutes up to ~17 minutes after
  "started" to finish creating their entities. `async_verify` was
  unaffected since it re-resolves targets fresh on every call — which is
  exactly why it could report the correct answer for entities nothing ever
  actually corrected live.

  Adds a periodic discovery sweep (`_async_scan_for_new_targets`, every
  `NEW_TARGET_SCAN_INTERVAL_SECONDS`) that re-resolves targets, folds any
  newly-matching entities into `self._known_targets`, resubscribes both
  persistent listeners against the enlarged set, and drives the new
  entities through the same batched `_drain_reregister_burst` pipeline a
  runtime re-registration burst already uses. A periodic sweep rather than
  an event-driven listener was a deliberate choice: the failure mode is
  minutes-scale (matching the existing `RETRY_DELAYS` timescale), so a
  sweep closes essentially the whole gap without adding a new, permanently
  subscribed global `state_changed` listener running on every state change
  instance-wide for the entry's whole lifetime. Also fixes a related gap
  where a domain/label/area-scoped config matching zero entities *at boot*
  (that domain's integration hasn't created anything yet at all) used to
  skip setting up the persistent listeners entirely — silently defeating
  the sweep for exactly the installs it exists to help.

## [0.9.6] — 2026-08-18
### Changed
- **`_patch_all_pending` (the scheduled/manual retry sweep of `self._pending`)
  now re-checks each entity's pending membership immediately before
  querying it, instead of only once when the sweep's snapshot was taken.**
  A scheduled retry pass and the boot-pending recovery burst's drain (see
  below) are both coroutines that yield control at their own await points,
  so they can run concurrently: without the re-check, a retry pass working
  through its snapshot could still call into an entity the drain had
  already resolved and discarded moments earlier, issuing a wasted
  redundant recorder query for it. Existing margin/bounded-run checks in
  `_resolve()` already prevented this from producing a *wrong* value, but
  the redundant work was needless recorder load that grows with how often
  the two paths overlap.

- **The boot-pending recovery listener (entities still unavailable/unknown
  when the boot pass ran) now coalesces unavailable→real transitions into a
  single batched drain, the same treatment already given to the
  re-registration listener above.** Previously, every transition
  immediately spawned its own task issuing its own targeted, per-entity
  recorder query (`_patch_pending`, always querying without bulk data). A
  mass recovery early in boot — a Zigbee/Z-Wave mesh finishing formation and
  announcing many devices within the same few seconds — fired one recorder
  query per entity all at once, and each of those queries used the raw,
  undeduplicated deep query rather than the recorder's genuine-value-change
  bulk query, the same correctness gap fixed for re-registrations above.

  Transitions are now debounce-coalesced (`PENDING_RECOVERY_DEBOUNCE_SECONDS`,
  capped by `PENDING_RECOVERY_MAX_WAIT_SECONDS`) into a burst set, then
  drained together via `_drain_pending_recovery_burst`, which reuses the
  boot pass's streaming bulk-query machinery (`_iter_bulk_batches`). Unlike
  the re-registration burst there's no separate per-entity retry ladder
  here: an entity the drain can't resolve simply stays in `_pending`, same
  as it always has, for the existing scheduled retry passes
  (`_schedule_retries` / `_patch_all_pending`) to pick up later.

- **The streamed bulk recorder query (`_iter_bulk_batches`, shared by the
  boot pass, `verify`, and the re-registration/pending-recovery burst
  drains) now paces itself with a zero-delay `asyncio.sleep(0)` after each
  batch.** A large "track all entities" install can mean many batches back
  to back, each a synchronous query on the recorder's own single-worker
  executor; without a yield point between them, the pass's own coroutine
  was immediately ready again the instant one batch resolved, which could
  crowd out other ready callbacks on the main event loop for the whole
  pass. `sleep(0)` is a pure yield with no added delay — pacing this way
  costs nothing in wall-clock time, it only gives other already-ready work
  a fair turn between batches.

- **Runtime re-registrations (a config entry reload, a Zigbee/Z-Wave device
  or coordinator rejoining) are now coalesced into a single batched drain
  instead of firing one recorder query per entity.** Previously, every
  re-registration event immediately spawned its own task, and each task ran
  its own targeted, per-entity recorder query
  (`_attempt_reregister_patch`). A mass re-registration — a hub's config
  entry reloading with hundreds of entities at once, or a coordinator
  reconnecting after an outage and re-registering everything it owns —
  therefore fired hundreds of concurrent tasks, each issuing its own
  recorder query: a thundering herd on the recorder executor with no
  batching at all, unlike the boot pass (which has used a streamed bulk
  query since 0.9.4).

  Re-registrations are now debounce-coalesced (`REREGISTER_DEBOUNCE_SECONDS`,
  capped by `REREGISTER_MAX_WAIT_SECONDS` for a continuously-arriving burst,
  the same debounce/max-wait shape already used for the incremental
  store) into a burst set, then drained together via
  `_drain_reregister_burst` — which reuses the boot pass's streaming bulk
  query machinery (`_iter_bulk_batches`) instead of querying one entity at a
  time. A mass re-registration now costs a handful of batched queries
  (bounded by `bulk_batch_size`) instead of one query per entity.

  Entities that don't resolve from the batched drain still get the same
  per-entity retry ladder as before (`retry_delays`) — retries are already
  spread out in time and aren't the source of the thundering-herd risk, so
  that path is unchanged. A single, isolated re-registration (the common
  case) still resolves quickly; it just now waits out the short debounce
  window first rather than being patched on the very next event loop tick.

### Fixed
- **An entity recovering from `unavailable`/`unknown` to a real value during
  normal runtime (not a restart, not a full re-registration) reset
  `last_changed` the same way a restart does, but nothing ever corrected it
  back.** HA's own state machine treats `unavailable`/`unknown` as a
  distinct value, so a brief network blip, Zigbee/Z-Wave mesh hiccup, or
  coordinator reconnect that took a device briefly offline and back
  genuinely bumped its `last_changed` to the recovery moment — even though,
  from this integration's point of view, that's exactly the same kind of
  artifact a restart produces. Unlike a restart, nothing previously
  re-examined that entity afterwards: the boot pass only runs once at
  startup, and the re-registration listener only fired when an entity was
  fully re-created (`old_state is None`), not when it merely recovered from
  an invalid state. Left uncorrected, an entity that flaps `unavailable`
  periodically — common for exactly this class of device — drifted wrong
  forever, self-healing only at the next full HA restart.

  Field-diagnosed on the same real installation as 0.9.3/0.9.5: a fresh
  `verify` pass reported 811 mismatches out of 3,450 checked entities.
  Cross-referencing each mismatch against the recorder database (read-only)
  showed 734 of the 811 (90.5%, spread across 16 different domains) were
  immediately preceded by an `unavailable`/`unknown` row — not a boot reset,
  not a full re-registration.

  The re-registration listener now also fires on recovery from
  `unavailable`/`unknown` (`old_state.state in INVALID_STATES`), routing it
  through the same debounced batched drain as re-registrations. The
  incremental store listener (`_on_target_state_changed`) now excludes this
  same transition from its debounce merge — it was previously treating a
  recovery-from-unavailable exactly like a genuine value change, which would
  have written the raw reset artifact into the snapshot store as if it were
  real, poisoning it for the next boot even after the live value was
  corrected. An entity is now marked `_unconfirmed` the moment it's queued
  for the drain (not just after a failed attempt), closing a narrow race
  where a snapshot write during the debounce window could persist the reset
  artifact before the drain had a chance to correct it.


## [0.9.5] — 2026-08-17
### Fixed
- **The snapshot store could get permanently poisoned with a restart
  artifact, causing `last_changed` to stay wrong across every subsequent
  restart.** Every HA restart resets `last_changed` to the restart moment
  for all entities before this integration gets a chance to correct it. If
  `_resolve()` couldn't find/apply a real timestamp for a given candidate
  during some boot pass — for any reason (a recorder race, a legitimate
  "just changed, too recent" decline, or anything else) — that entity's
  live `last_changed` stayed at the raw, uncorrected restart-reset value
  for the rest of that session. The periodic snapshot timer and the
  clean-shutdown snapshot write both read `live.last_changed` straight off
  the state machine with no way to tell a confirmed real value from an
  unconfirmed artifact, so that uncorrected value got written into the
  persistent snapshot store as if it were genuine.

  From that point on the wrong value was self-reinforcing: on every future
  boot, `_resolve()`'s snapshot check (step 2, checked before the deep/
  best-effort steps that would find the real answer) saw the entity still
  holding the same value, and the poisoned timestamp was — by definition —
  older than the *new* restart's fresh cutoff, so it comfortably cleared
  the margin check and got confidently reapplied. This happened silently,
  every single boot, without ever reaching the recorder-query steps that
  would have found the true value, and it was completely independent of
  (and unaffected by) the 0.9.3 purge-boundary fix, which only touches the
  best-effort unbounded step, not the snapshot short-circuit ahead of it.

  Field-diagnosed on the same real installation as 0.9.3: after upgrading,
  a `verify` pass still reported ~1,600 mismatches, barely down from the
  pre-0.9.3 count. Direct (read-only) queries against the installation's
  recorder database confirmed the mechanism precisely: entities' *live*
  `last_changed` matched one of their own earlier restart's own artifact
  rows still sitting in the database, while `verify`'s freshly recomputed
  answer matched the oldest surviving row for that entity exactly to the
  microsecond — and the installation's own `.storage/last_changed_keeper.snapshot`
  held that same wrong, restart-artifact timestamp for the affected
  entities.

  `_resolve()` no longer accepts blame for this: entities whose live
  `last_changed` is still an unconfirmed restart artifact are now tracked
  (`_unconfirmed`, cleared on any successful patch or genuine value change),
  and both snapshot write paths preserve an unconfirmed entity's *existing*
  stored entry (or leave it unwritten if there isn't one) instead of
  overwriting it with the unverified live value.

  **This does not retroactively fix an already-poisoned snapshot** — the
  fix only stops *new* poisoning. If you're upgrading from an affected
  version, clear `.storage/last_changed_keeper.snapshot` once (with Home
  Assistant stopped) to let it rebuild cleanly through the now-safe resolve
  chain; leaving it in place means already-wrong entities stay wrong until
  their value genuinely changes for real.

## [0.9.4] — 2026-08-17
### Fixed
- **Unbounded rows per entity in the bulk pass — the remaining OOM driver
  (#1).** v0.9.2's streaming bounded the boot/verify pass at one *batch*,
  but a batch was still unbounded in *rows*: `get_significant_states` has
  no per-entity limit, so a single chatty power sensor contributed its
  entire 30-day history (10⁵–10⁶ rows, ~0.7 GB per million rows measured
  against a real recorder DB) to its batch regardless of batch size — and
  for entities in the recorder's hard-coded significant domains (`climate`,
  `device_tracker`, `humidifier`, `thermostat`, `water_heater`) even
  attribute-only rows came back unfiltered. Both effects were confirmed
  empirically (20 000 inserted rows → 20 000 returned, in all three cases).

  The bulk query is now a direct window-function query over the recorder's
  `states`/`states_meta` tables: only genuine value-change rows (for every
  domain), capped at the newest `BULK_PER_ENTITY_LIMIT` (100) rows per
  entity, returned as lightweight rows instead of full `State` objects. A
  batch is now bounded at `bulk_batch_size × 100` small rows (≈ a few MB)
  no matter how chatty the installation. `unavailable`/`unknown` rows are
  excluded at the SQL layer so availability flapping cannot push a run's
  bounding row out of the capped result. The per-entity resolve walk now
  also runs over at most 100 rows, removing the multi-hundred-ms event-loop
  stalls that the old per-entity `sorted()` over full histories caused.
- **Depth-cap trust rule for the capped bulk result.** A full-length
  (100-row) *unbounded* run means the cap truncated the history, not that
  the run's true origin was reached — such results are now discarded in the
  best-effort path, mirroring the existing `HISTORY_DEPTH` rule for the
  per-entity deep query. Bounded runs are unaffected: truncation can only
  ever hide a bounding row, never fabricate one.

### Changed
- Attribute-noisy entities in the recorder's significant domains now
  resolve exactly like every other domain (their attribute chatter
  previously pushed the old query's unbounded best-effort timestamp to the
  bulk-window edge, where the purge-boundary guard usually discarded it;
  with chatter gone, their genuine rows resolve under the same best-effort
  rules as all other entities).
- The `states`/`states_meta` model import is guarded: if a future Home
  Assistant release relocates the internal `db_schema` module, the bulk
  pass degrades to the per-batch fallback (snapshot / capped deep query)
  instead of failing to load.
- `bulk_rows_fetched` in the run stats now counts genuine value-change
  rows only, so its numbers drop sharply compared to earlier releases —
  that's the fix working, not missing data.

## [0.9.3] — 2026-08-15
### Fixed
- **Confidently-wrong `last_changed` on entities older than the recorder's
  purge window.** Every HA restart writes a fresh recorder row for each
  entity even when its value hasn't changed — the live in-place
  `last_changed` patch this integration applies never gets written back to
  the recorder database, so the raw "reset to restart time" row is always
  there for the next pass to see. `_resolve`'s best-effort unbounded path
  (step 4) is specifically designed to walk straight through those
  same-value restart rows and keep looking for a genuinely older value, but
  once `purge_keep_days` has erased the earlier restarts, "ran out of rows"
  and "found the real origin" are indistinguishable from inside a single
  entity's own history. On an install with several restarts inside the
  retention window and typical retention shorter than how long some
  entities go without a real change (automations never disabled, alerts
  that have never fired, long-clear presence sensors), this reliably landed
  on the oldest *surviving restart artifact* and reported it as the genuine
  timestamp — sometimes applied directly at boot, sometimes only surfaced
  later by `verify` once further purge attrition flipped an entity from
  "correctly declined" (blocked by the existing `HISTORY_DEPTH` guard) to
  "confidently wrong," with no real change to the entity in between. Worse,
  the wrong answer wasn't stable: each restart that outlived the retention
  window could drag the same entity's `last_changed` forward again, to
  whichever restart artifact was now oldest.

  `_resolve` now reads the recorder's actual retention boundary directly
  (`Recorder.keep_days`/`auto_purge`, rather than inferring it by querying
  the database) and discards — rather than applies — any unbounded result
  landing at or before that boundary (`PURGE_BOUNDARY_MARGIN_DAYS` absorbs
  purge running periodically rather than continuously). Field-diagnosed
  against a real ~3,500-entity installation: a `verify` pass reported 1,693
  mismatches, the great majority clustered within a couple of seconds of a
  single timestamp that was exactly `purge_keep_days` (14 in this case) old
  — a previous restart, not a real event, confirmed by cross-checking
  against the oldest surviving row in the recorder database.

  **Trade-off, worth understanding before upgrading:** this closes a class
  of silent wrong answers, but it cannot recover them — the information the
  fix needs is exactly what purge already deleted. For any entity whose
  true last real change predates `purge_keep_days`, this integration will
  now leave `last_changed` at the restart-reset value indefinitely (same as
  if it weren't installed for that entity) rather than guess, until either
  the entity genuinely changes again or retention is extended past however
  long that entity tends to stay unchanged. In practice this most affects
  exactly the "very stable, long-lived" entities this integration is often
  installed to help with, so the honest "don't know" is the safer default,
  not a full fix for the underlying information gap. A `verify` mismatch
  count after upgrading should drop accordingly, but a lower count here
  reflects fewer *false positives*, not more entities actually restored.

## [0.9.2] — 2026-08-14
### Fixed
- **Unbounded memory growth during the bulk recorder pass.** The bulk query
  was chunked by batch size, but every chunk was still merged into one dict
  held live for the whole resolve pass — on a large "track all entities"
  install the merged result is every significant state change of every
  entity over the 30-day bulk window, easily gigabytes, and the `verify`
  service (which checks every target, not just fresh boot candidates) was
  the worst case. The bulk fetch is now a streaming generator
  (`_iter_bulk_batches`): each batch is resolved and dropped before the
  next query runs, so peak memory stays at roughly one batch instead of the
  whole installation. The default batch size is lowered from 500 to 250
  now that it is the direct peak-memory knob (configurable per entry since
  0.9.1).
- **Exponential retry fan-out on repeated re-registration failure.** A
  runtime re-registration (config entry reload, a Zigbee/Z-Wave device
  rejoining) that failed to resolve on a retry used to arm a *fresh* ladder
  of `len(retry_delays)` timers on top of the ones already running, instead
  of replacing them — so an entity that kept failing to resolve multiplied
  its in-flight recorder queries roughly 1.5× every 30 s until the grace
  window closed, rather than staying capped at one ladder per
  re-registration. Retries are now armed at exactly one entry point.
- **Boot-pass restore time regression** introduced by the streaming fix
  above: because a streamed pass can itself take a while, the per-batch
  re-validation used to compare elapsed time since boot against `grace`
  again, which — for an entity that never actually changed — really just
  measured how long the pass itself had been running. Once that elapsed
  time crossed `grace`, every remaining candidate in a slow pass was
  silently skipped and never retried. The re-check now compares against
  each candidate's `last_changed` as snapshotted when the candidate list
  was built, which is what it was always meant to measure.

### Added
- **Recorder-cost instrumentation.** Boot-pass stats (and the `verify`
  service response) now include `bulk_rows_fetched`, `bulk_batches`, and
  `deep_queries` (prefixed `verify_*` for the post-boot self-check) —
  visible on the status sensor/diagnostics, so the recorder cost of a pass
  is inspectable without inferring it from wall-clock duration.

### Changed
- The incremental snapshot store's debounced write now goes through
  `Store.async_delay_save` (already used for the run-history store) instead
  of an untracked `async_create_task` per flush, so closely-spaced flushes
  coalesce into one write.

Ported from field-validated fixes developed and tested against a real
~3,500-entity installation at
[cerebrate/last_changed_keeper#1](https://github.com/cerebrate/last_changed_keeper/pull/1)
and [#2](https://github.com/cerebrate/last_changed_keeper/pull/2) — see
GitHub issue #1 for the investigation history.

## [0.9.1] — 2026-08-14
### Added
- **Configurable bulk-query batch size.** The 500-entities-per-query batch
  size introduced in 0.9.0 is now a setup/reconfigure/options field
  (default unchanged at 500). Installs with very large tracked-entity
  counts that still see high memory use during the bulk pass can lower it;
  the merged result of a batch is still held for the whole resolve pass, so
  this is a mitigation, not a fix, for the underlying memory-growth issue —
  see GitHub issue #1 for the ongoing investigation of a proper streaming
  fix.

## [0.9.0] — 2026-08-05
### Added
- **Status sensor.** A diagnostic sensor ("Restored entities") whose state is
  the number of entities patched in the last pass, with the full run stats,
  the degraded flag and the run history as attributes — the diagnostics
  data, visible on a dashboard without downloading anything.
- **Rolling run history.** The stats of the last 10 boot passes are persisted
  (`.storage/last_changed_keeper.runs`) and exposed on the status sensor —
  "how well did restore work over the last restarts" at a glance.
- **Optional post-boot self-check.** A new toggle runs a `verify` pass a few
  minutes after the boot pass and logs a warning listing any entities whose
  live `last_changed` deviates from the recorder/store-derived value —
  catches "the restore silently did nothing" without waiting for someone to
  notice wrong timestamps. Off by default.

### Changed
- **Bulk recorder query is now batched** (500 entities per query). With
  "track all entities" the candidate list can be thousands of entities; one
  giant `IN (...)` query gets slow and memory-hungry, and a single failure
  used to lose the whole bulk result — now it only loses that batch.

## [0.8.0] — 2026-08-05
### Added
- **"Track all entities" toggle.** A new switch at the top of the setup /
  reconfigure / options form covers every entity in Home Assistant,
  regardless of domains/entities/labels/areas, and is now **on by default**.
  Picking specific domains or entities still works exactly as before — just
  turn the toggle off first. Previously, at least one domain or entity had
  to be selected or the form rejected the save with "please select at least
  one domain or one entity", which was needless friction for the common
  case of just wanting everything covered.

## [0.7.0] — 2026-07-22
### Added
- **Re-patch on runtime re-registration.** Previously, only a full HA restart
  was recognised as the "reset to now" event. A persistent, entry-lifetime
  listener (independent of the boot-time pending/listener machinery) now
  also catches an already-watched entity being fully re-created afterwards
  — its owning config entry reloading, a Zigbee/Z-Wave device rejoining, or
  the entity briefly disappearing and reappearing — and re-patches just
  that one entity with a targeted per-entity query, respecting the same
  grace window and retry delays as the boot pass, without slowing down boot
  or re-running the full bulk query.
- **`last_triggered` restoration for automations/scripts.** A second, clearly
  separate patch path (own recorder read, own apply mechanism, since
  `last_triggered` is an attribute, not the state value) restores it for
  `automation.*`/`script.*` entities when Home Assistant's own restore
  mechanism didn't (crash, purged restore-state cache, long outage). New
  **restore automation/script `last_triggered`** option (default: on).
- **Incremental runtime store.** Every genuine value change of a watched
  entity is now debounced (~8 s, capped at 30 s under continuous chatter)
  into the same store used for the periodic/shutdown snapshot, instead of
  only updating it every `snapshot_interval` seconds or at shutdown. Only
  one entry per entity is kept (merged, not appended), so this does not
  grow unbounded. `_resolve()` now prefers this store's value over an
  otherwise-definitive bulk result when it holds a newer, still-usable
  timestamp for the same value (e.g. recorder commit lag).
- **`last_changed_keeper.verify` service** (`supports_response: only`):
  compares the live `last_changed` of every currently watched entity against
  the recorder/store-derived real value and returns any mismatches
  (`entity_id`, `live_last_changed`, `expected_last_changed`,
  `diff_seconds`) without patching anything — for diagnosing "the value
  looks wrong" reports.
- Swedish (`sv`), Czech (`cs`), Norwegian Bokmål (`nb`) and Danish (`da`)
  translations, following the existing 7 non-English languages.
- `icons.json` for the `restore_now`/`verify` services.
### Changed
- `quality_scale` raised to **gold**. See the README's "Quality scale"
  section for the honest per-rule breakdown — several gold rules
  (`devices`, `entity-category`, `entity-translations`, `discovery`,
  `stale-devices`, ...) don't apply, since this integration has no entities
  or devices of its own.
- README: added "Use cases", "How data is refreshed", "Examples" and
  "Troubleshooting" sections; documented the last_changed/last_triggered
  distinction and the runtime re-registration/incremental-store behavior.
### Tests
- Added `tests/test_reregistration.py`, `tests/test_last_triggered.py`,
  `tests/test_incremental_store.py` and `tests/test_verify_service.py`
  (28 new tests), plus a config-flow schema test for the new
  `restore_last_triggered` field.

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
