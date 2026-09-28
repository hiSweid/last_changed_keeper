"""Tests for the per-entity row cap of the bulk query.

The bulk query fetches at most BULK_PER_ENTITY_LIMIT of an entity's newest
genuine value-change rows and drops attribute-only rows for EVERY domain —
including the recorder's SIGNIFICANT_DOMAINS (climate, device_tracker, ...),
whose attribute-only rows get_significant_states used to return unfiltered.
Both properties are what bound a batch's memory (see the OOM issue #1); they
are pinned here against a REAL recorder the same way
test_resolve_equivalence.py does, plus synthetic tests for the depth-cap
trust rule in _resolve step 4.
"""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _BulkRow, _RestoreJob
from custom_components.last_changed_keeper.const import (
    BULK_PER_ENTITY_LIMIT,
    BULK_WINDOW_DAYS,
    DOMAIN,
    MARGIN_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, {})


async def _run_bulk(job: _RestoreJob, entity_id: str) -> list:
    async for _chunk, bulk in job._iter_bulk_batches([entity_id]):
        return bulk.get(entity_id) or []
    raise AssertionError("_iter_bulk_batches yielded no batch")


async def test_cap_keeps_only_newest_rows(recorder_mock, hass: HomeAssistant) -> None:
    """More genuine value changes than the cap: exactly BULK_PER_ENTITY_LIMIT
    rows come back, and they are the NEWEST ones (the walk needs the newest
    run plus its bounding row; older rows are dead weight)."""
    now = dt_util.utcnow()
    total = BULK_PER_ENTITY_LIMIT + 25
    base = now - timedelta(days=2)
    for i in range(total):
        with freeze_time(base + timedelta(minutes=i)):
            hass.states.async_set("sensor.chatty_value", str(i))
    await async_wait_recording_done(hass)

    rows = await _run_bulk(_make_job(hass), "sensor.chatty_value")

    assert len(rows) == BULK_PER_ENTITY_LIMIT
    states = {r.state for r in rows}
    # The newest `total` writes are 0..total-1; the cap must keep the newest
    # BULK_PER_ENTITY_LIMIT of them, i.e. values 25..total-1, not 0..99.
    assert str(total - 1) in states
    assert "0" not in states


async def test_attribute_only_rows_excluded_for_significant_domain(
    recorder_mock, hass: HomeAssistant
) -> None:
    """device_tracker is one of the recorder's SIGNIFICANT_DOMAINS, whose
    attribute-only rows get_significant_states returns unfiltered — the
    empirically confirmed OOM amplifier. The capped query must drop them:
    only genuine value changes come back, and the resolve outcome (run start
    = the real home transition) is unaffected by attribute chatter."""
    now = dt_util.utcnow()
    went_home_at = now - timedelta(days=3)

    with freeze_time(went_home_at - timedelta(hours=1)):
        hass.states.async_set("device_tracker.phone", "not_home")
    with freeze_time(went_home_at):
        hass.states.async_set("device_tracker.phone", "home")
    # Attribute-only chatter: same state, changing attributes -> recorder
    # rows whose last_changed stays at went_home_at.
    for i in range(30):
        with freeze_time(went_home_at + timedelta(minutes=i + 1)):
            hass.states.async_set(
                "device_tracker.phone", "home", {"gps_accuracy": i}
            )
    await async_wait_recording_done(hass)

    rows = await _run_bulk(_make_job(hass), "device_tracker.phone")

    # 2 genuine changes (not_home, home) - not 32 rows.
    assert len(rows) == 2
    ts, bounded = lck._real_last_changed(rows, "home")
    assert bounded
    assert abs((ts - went_home_at).total_seconds()) < 1


async def test_resolve_distrusts_depth_capped_bulk_run(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An unbounded bulk run of exactly BULK_PER_ENTITY_LIMIT rows means the
    window function truncated the history — the oldest row is NOT the run's
    true start and must not be applied (same rule as HISTORY_DEPTH for the
    deep query). One row fewer means the window was genuinely exhausted and
    the best-effort result stays usable. The recorder DB is empty here, so
    the deep query finds nothing and step 4 falls back to the bulk result."""
    job = _make_job(hass)
    now = dt_util.utcnow()
    hass.states.async_set("sensor.capped", "on")
    await hass.async_block_till_done()
    live = hass.states.get("sensor.capped")

    def _rows(count: int) -> list[_BulkRow]:
        return [
            _BulkRow(
                "on",
                now - timedelta(days=1, minutes=i),
                now - timedelta(days=1, minutes=i),
            )
            for i in range(count)
        ]

    capped = await job._resolve("sensor.capped", live, _rows(BULK_PER_ENTITY_LIMIT))
    below_cap = await job._resolve(
        "sensor.capped", live, _rows(BULK_PER_ENTITY_LIMIT - 1)
    )

    assert capped is None
    assert below_cap is not None


async def test_bounded_result_at_exact_cap_length_stays_trusted(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Truncation can only ever produce a spuriously UNBOUNDED result, never
    a false boundary — a result of exactly BULK_PER_ENTITY_LIMIT rows that
    still bounds within them found a genuine differing older value and must
    keep resolving normally. Pins that the depth-cap distrust applies ONLY
    to unbounded runs, not to every full-length result.

    The attribute-only noise after the last genuine change saturates the
    per-entity DEEP query (raw rows, HISTORY_DEPTH-capped) so it cannot
    rescue the resolve — the correct answer is reachable exclusively via
    the bulk result's bounded run, which is exactly the property under
    test."""
    now = dt_util.utcnow()
    total = BULK_PER_ENTITY_LIMIT + 30
    base = now - timedelta(days=1)
    for i in range(total):
        with freeze_time(base + timedelta(minutes=i)):
            hass.states.async_set("light.busy", "on" if i % 2 else "off")
    final_value = "on" if (total - 1) % 2 else "off"
    final_ts = base + timedelta(minutes=total - 1)
    noise_base = base + timedelta(minutes=total)
    for i in range(110):
        with freeze_time(noise_base + timedelta(seconds=30 * i)):
            hass.states.async_set("light.busy", final_value, {"noise": i})
    await async_wait_recording_done(hass)

    hass.states._states_data.pop("light.busy", None)
    hass.states.async_set("light.busy", final_value)
    live = hass.states.get("light.busy")
    await hass.async_block_till_done()

    job = _make_job(hass)
    rows = await _run_bulk(job, "light.busy")
    # The boot-reset write above is itself a real recorder row, so the cap
    # window shifts by one — what matters is that the result IS full-length
    # and still bounds.
    assert len(rows) == BULK_PER_ENTITY_LIMIT
    _ts, bounded = lck._real_last_changed(rows, final_value)
    assert bounded

    result = await job._resolve("light.busy", live, rows)
    assert result is not None
    assert abs((result - final_ts).total_seconds()) < 1
    assert (dt_util.utcnow() - result).total_seconds() > MARGIN_SECONDS


async def test_cap_is_per_entity_not_per_batch(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The window function partitions by entity: one chatty entity in a
    batch must not push another entity's (older) rows out of the result.
    Without partition_by, a global row_number over the batch would assign
    the quiet entity's older rows ranks beyond the cap and drop them."""
    now = dt_util.utcnow()
    base = now - timedelta(days=1)
    for i in range(BULK_PER_ENTITY_LIMIT + 20):
        with freeze_time(base + timedelta(minutes=i)):
            hass.states.async_set("sensor.loud", str(i))
    quiet_changed_at = now - timedelta(days=10)
    with freeze_time(quiet_changed_at - timedelta(hours=1)):
        hass.states.async_set("switch.quiet", "off")
    with freeze_time(quiet_changed_at):
        hass.states.async_set("switch.quiet", "on")
    await async_wait_recording_done(hass)

    job = _make_job(hass)
    async for _chunk, bulk in job._iter_bulk_batches(
        ["sensor.loud", "switch.quiet"]
    ):
        assert len(bulk["sensor.loud"]) == BULK_PER_ENTITY_LIMIT
        assert len(bulk["switch.quiet"]) == 2
        ts, bounded = lck._real_last_changed(bulk["switch.quiet"], "on")
        assert bounded
        assert abs((ts - quiet_changed_at).total_seconds()) < 1


async def test_availability_flaps_do_not_consume_cap(
    recorder_mock, hass: HomeAssistant
) -> None:
    """unavailable/unknown rows are genuine value changes in the recorder
    but are discarded unread by the resolve walk — they must be filtered at
    the SQL layer so a flaky device's availability flapping cannot push the
    run's bounding row out of the capped result. Regression test for the
    review finding: 60 flap cycles after a genuine change 5 days ago left
    the unfiltered query with 100 flap rows and no bound -> no restore."""
    now = dt_util.utcnow()
    changed_at = now - timedelta(days=5)

    with freeze_time(changed_at - timedelta(hours=1)):
        hass.states.async_set("light.flaky", "off")
    with freeze_time(changed_at):
        hass.states.async_set("light.flaky", "on")
    flap_base = now - timedelta(days=2)
    for i in range(60):
        with freeze_time(flap_base + timedelta(minutes=2 * i)):
            hass.states.async_set("light.flaky", "unavailable")
        with freeze_time(flap_base + timedelta(minutes=2 * i + 1)):
            hass.states.async_set("light.flaky", "on")
    await async_wait_recording_done(hass)

    job = _make_job(hass)
    rows = await _run_bulk(job, "light.flaky")

    assert len(rows) < BULK_PER_ENTITY_LIMIT
    assert not any(r.state in ("unavailable", "unknown") for r in rows)
    ts, bounded = lck._real_last_changed(rows, "on")
    assert bounded
    assert abs((ts - changed_at).total_seconds()) < 1


async def test_bulk_window_still_applies(recorder_mock, hass: HomeAssistant) -> None:
    """Rows older than BULK_WINDOW_DAYS stay out of the bulk result (the
    deep query handles those, unchanged)."""
    now = dt_util.utcnow()
    outside = now - timedelta(days=BULK_WINDOW_DAYS + 5)

    with freeze_time(outside):
        hass.states.async_set("switch.old", "on")
    await async_wait_recording_done(hass)

    rows = await _run_bulk(_make_job(hass), "switch.old")
    assert rows == []


async def test_recreation_rows_do_not_consume_cap_slots(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Field-diagnosed regression: an entity repeatedly torn down and
    recreated with an UNCHANGED value (Entity.async_remove() then
    async_set() with the same value - an ordinary restart, a
    config-entry reload, or a cascade from an unrelated integration's own
    instability) must not have those recreation rows count toward
    BULK_PER_ENTITY_LIMIT. Left uncapped-on-transitions, enough
    recreation cycles push the genuine earlier transition out of the
    capped window entirely, and the resolve walk finds no differing
    value at all (bounded=False) instead of the true, stable origin."""
    now = dt_util.utcnow()
    genuine_change_at = now - timedelta(days=5)

    with freeze_time(genuine_change_at - timedelta(minutes=1)):
        hass.states.async_set("binary_sensor.flaky_device", "off")
    with freeze_time(genuine_change_at):
        hass.states.async_set("binary_sensor.flaky_device", "on")

    # More recreation cycles than the cap - without dedup, these alone
    # would fill (and overflow) BULK_PER_ENTITY_LIMIT, pushing the
    # genuine transition above out of the capped window.
    recreate_base = now - timedelta(hours=2)
    for i in range(BULK_PER_ENTITY_LIMIT + 20):
        with freeze_time(recreate_base + timedelta(seconds=i * 10)):
            hass.states.async_remove("binary_sensor.flaky_device")
            hass.states.async_set("binary_sensor.flaky_device", "on")
    await async_wait_recording_done(hass)

    rows = await _run_bulk(_make_job(hass), "binary_sensor.flaky_device")

    ts, bounded = lck._real_last_changed(rows, "on")
    assert bounded, "recreation noise crowded out the genuine transition"
    assert abs((ts - genuine_change_at).total_seconds()) < 1


async def test_genuine_alternating_transitions_are_not_collapsed(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The dedup step must only remove true duplicates - a real
    back-and-forth pattern (more transitions than the cap) still fills
    the cap with genuine transitions, same as before this change."""
    now = dt_util.utcnow()
    total = BULK_PER_ENTITY_LIMIT + 25
    base = now - timedelta(days=2)
    for i in range(total):
        with freeze_time(base + timedelta(minutes=i)):
            hass.states.async_set(
                "binary_sensor.alternator", "on" if i % 2 == 0 else "off"
            )
    await async_wait_recording_done(hass)

    rows = await _run_bulk(_make_job(hass), "binary_sensor.alternator")

    assert len(rows) == BULK_PER_ENTITY_LIMIT
    # The newest `total` writes alternate on/off; the cap must keep the
    # newest BULK_PER_ENTITY_LIMIT of them (values from index 25 on), not
    # collapse the alternation away.
    oldest_kept = min(rows, key=lambda r: r.last_updated)
    expected_oldest = base + timedelta(minutes=total - BULK_PER_ENTITY_LIMIT)
    assert abs((oldest_kept.last_updated - expected_oldest).total_seconds()) < 1
