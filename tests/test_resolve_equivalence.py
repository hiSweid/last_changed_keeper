"""Equivalence tests for the resolve chain against a REAL recorder.

Every other test touching _resolve (test_resolve.py, test_real_last_changed.py,
test_incremental_store.py, ...) monkeypatches _bulk_query/get_last_state_changes
or hands _resolve synthetic FakeRow history directly. That's the right choice
for testing the pure decision logic - but it means nothing in the suite
exercises the actual round trip: a real recorder-backed bulk query for the
current BULK_WINDOW_DAYS window, retrieving genuinely written historical
rows, feeding into _resolve exactly as _async_run_impl/async_verify do in
production.

That round trip is exactly the boundary any future change to *how* history is
fetched (e.g. querying a short window first and escalating only for entities
still unbounded, instead of one flat BULK_WINDOW_DAYS query) would touch. These
tests pin FINAL OUTCOMES - the timestamp _resolve ultimately returns for an
entity at a known real distance into the past - rather than how the data was
obtained, so they should keep passing unchanged across such a change: that's
what makes them useful as a safety net for it rather than a description of
today's specific fetch strategy.

Historical states are written via freeze_time (moving the actual clock
dt_util.utcnow() reads, not just passing a timestamp kwarg) plus
async_wait_recording_done, so they land in the recorder with genuine
last_changed/last_updated values rather than values inferred after the fact -
the same technique pytest_homeassistant_custom_component's own recorder test
helpers use (see record_states in its recorder test-common module).
"""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
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


def _simulate_boot_reset(hass: HomeAssistant, entity_id: str, value: str) -> State:
    """Simulate what a real HA restart does to last_changed, using genuine
    recorder history written beforehand.

    A fresh HA process has no in-memory State for any entity, so the first
    async_set after boot is unconditionally treated as new (last_changed =
    now) - unlike a same-value async_set in a continuously-running process,
    which StateMachine.async_set_internal deliberately preserves
    last_changed across (`last_changed = old_state.last_changed if
    same_state`). Popping the state machine's internal dict entry directly
    reproduces exactly that "no memory of this entity" condition.

    async_remove() is the more obvious way to clear it, and is what
    test_reregistration.py uses to simulate a *different* scenario (a
    runtime re-registration, where the recorder genuinely is running and
    does log the disappearance). It isn't needed here: a real restart
    never produces a recorder row for the gap at all (the recorder isn't
    running during it, so there's no event to log) - just a temporal gap
    in the data, which _real_last_changed already handles correctly (see
    test_real_last_changed.py's restart-recovery tests). A state=None row
    from async_remove() would also be fine to use here now - it's
    transparent to _real_last_changed's walk the same way unavailable/
    unknown are (see test_real_last_changed.py's removal-row tests) - but
    popping the state machine's dict entry directly is simpler and closer
    to what a genuine restart does.
    """
    hass.states._states_data.pop(entity_id, None)
    hass.states.async_set(entity_id, value)
    return hass.states.get(entity_id)


async def _resolve_via_real_bulk(job: _RestoreJob, entity_id: str, live: State):
    """Resolve entity_id the same way _async_run_impl/async_verify do: a
    real (unmocked) bulk query for the current BULK_WINDOW_DAYS window,
    then _resolve with whatever it returns - as opposed to the rest of the
    suite, which hands _resolve synthetic rows directly."""
    async for _chunk, bulk in job._iter_bulk_batches([entity_id]):
        return await job._resolve(entity_id, live, bulk.get(entity_id))
    raise AssertionError("_iter_bulk_batches yielded no batch for one entity_id")


async def test_change_well_inside_bulk_window_resolves_via_real_bulk_query(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A change comfortably inside the BULK_WINDOW_DAYS window is found by
    the bulk query alone - the common case, and the cheapest path."""
    now = dt_util.utcnow()
    changed_at = now - timedelta(days=5)

    with freeze_time(now - timedelta(days=6)):
        hass.states.async_set("light.inside_window", "off")
        await async_wait_recording_done(hass)
    with freeze_time(changed_at):
        hass.states.async_set("light.inside_window", "on")
        await async_wait_recording_done(hass)

    live = _simulate_boot_reset(hass, "light.inside_window", "on")
    await hass.async_block_till_done()

    job = _make_job(hass)
    result = await _resolve_via_real_bulk(job, "light.inside_window", live)

    assert result is not None
    assert abs((result - changed_at).total_seconds()) < 1


async def test_change_beyond_bulk_window_falls_through_to_real_deep_query(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A change older than BULK_WINDOW_DAYS is invisible to the bulk query
    (nothing in that window to find) but is still recoverable via the
    per-entity deep query, which has no day-based cutoff - exactly the case
    a future staged-window-widening change must keep resolving correctly,
    since it's the scenario that motivates that change in the first place.
    """
    now = dt_util.utcnow()
    assert BULK_WINDOW_DAYS < 40, "test assumes days_ago below exceeds the window"
    changed_at = now - timedelta(days=40)

    with freeze_time(now - timedelta(days=41)):
        hass.states.async_set("light.beyond_window", "off")
        await async_wait_recording_done(hass)
    with freeze_time(changed_at):
        hass.states.async_set("light.beyond_window", "on")
        await async_wait_recording_done(hass)

    live = _simulate_boot_reset(hass, "light.beyond_window", "on")
    await hass.async_block_till_done()

    job = _make_job(hass)

    # Confirm the premise: the real bulk query must not itself produce a
    # bounded (definitive) answer for this entity, so the fallthrough below
    # is exercising the deep query, not silently getting lucky via bulk.
    # The reset write above (_simulate_boot_reset) happens at genuine
    # "now", outside any freeze_time - unlike a real restart, the recorder
    # here keeps running throughout the test, so that write is itself a
    # real, current row and does legitimately appear in the bulk result.
    # That's harmless: it's the newest of an unbroken "on" run with nothing
    # older/differing within the window, so _real_last_changed correctly
    # calls it unbounded rather than mistaking it for a definitive answer.
    async for _chunk, bulk in job._iter_bulk_batches(["light.beyond_window"]):
        _bulk_ts, bulk_bounded = lck._real_last_changed(
            bulk.get("light.beyond_window") or [], "on"
        )
        assert not bulk_bounded, "test setup: change must be outside the bulk window"

    result = await _resolve_via_real_bulk(job, "light.beyond_window", live)

    assert result is not None
    assert abs((result - changed_at).total_seconds()) < 1


async def test_restart_recovery_pattern_collapses_correctly_via_real_bulk_query(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Real recorder history containing genuine unavailable/recovery blips
    (a device dropping offline and reconnecting to the same value) must
    still collapse to the true original change time, not to the most
    recent recovery - mirrors test_real_last_changed.py's
    test_skips_restart_recovery, but through a real bulk query and real
    recorder-stored State objects rather than synthetic FakeRows, to
    confirm the actual SQL round trip preserves what that pure-logic test
    already established about the algorithm.
    """
    now = dt_util.utcnow()
    real_change = now - timedelta(days=10)

    with freeze_time(now - timedelta(days=11)):
        hass.states.async_set("light.flaky_zigbee", "off")
        await async_wait_recording_done(hass)
    with freeze_time(real_change):
        hass.states.async_set("light.flaky_zigbee", "on")
        await async_wait_recording_done(hass)
    with freeze_time(now - timedelta(days=9)):
        hass.states.async_set("light.flaky_zigbee", "unavailable")
        await async_wait_recording_done(hass)
    with freeze_time(now - timedelta(days=9) + timedelta(hours=1)):
        hass.states.async_set("light.flaky_zigbee", "on")  # recovery
        await async_wait_recording_done(hass)
    with freeze_time(now - timedelta(days=1)):
        hass.states.async_set("light.flaky_zigbee", "unavailable")
        await async_wait_recording_done(hass)
    with freeze_time(now - timedelta(days=1) + timedelta(hours=1)):
        hass.states.async_set("light.flaky_zigbee", "on")  # recovery
        await async_wait_recording_done(hass)

    live = _simulate_boot_reset(hass, "light.flaky_zigbee", "on")
    await hass.async_block_till_done()

    job = _make_job(hass)
    result = await _resolve_via_real_bulk(job, "light.flaky_zigbee", live)

    assert result is not None
    # Not the most recent recovery (1 day ago) or the first (9 days ago) -
    # the real change, 10 days ago.
    assert abs((result - real_change).total_seconds()) < 1


async def test_recent_real_change_within_margin_is_not_backdated(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A value that genuinely changed shortly before the reset must not be
    backdated - the bulk result is bounded and definitive, but too recent
    to clear MARGIN_SECONDS, so _resolve must return None rather than
    misapply a value the entity was never really at for long."""
    now = dt_util.utcnow()
    just_changed = now - timedelta(seconds=MARGIN_SECONDS / 2)

    with freeze_time(now - timedelta(hours=1)):
        hass.states.async_set("light.just_changed", "off")
        await async_wait_recording_done(hass)

    with freeze_time(just_changed):
        hass.states.async_set("light.just_changed", "on")
        await async_wait_recording_done(hass)
        # Reset while still frozen at the same instant as the real change,
        # so cutoff and the bulk-detected change are (deterministically,
        # regardless of real test execution speed) within MARGIN_SECONDS
        # of each other.
        live = _simulate_boot_reset(hass, "light.just_changed", "on")
        await hass.async_block_till_done()

    job = _make_job(hass)
    result = await _resolve_via_real_bulk(job, "light.just_changed", live)

    assert result is None
