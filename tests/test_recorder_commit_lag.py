"""Tests for _wait_for_recorder_commit: every recorder history query this
integration issues must not run ahead of the recorder's own write queue.
Recorder writes are committed asynchronously, so a state change already
visible via hass.states.get() can still be missing from a history query
for a short window (commit lag) - and _resolve's "bounded" trust treats
the instant a query returns any older, differently-valued row as
definitive, with no protection against the query having simply missed the
most recent, not-yet-committed transition.
"""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def test_wait_for_recorder_commit_returns_promptly_when_idle(
    recorder_mock, hass: HomeAssistant
) -> None:
    """With nothing queued (the common case), async_get_commit_future
    returns None and the helper must not introduce any real wait."""
    job = _make_job(hass)

    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)


async def test_wait_for_recorder_commit_waits_for_pending_commit(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """A pending commit future must genuinely be awaited before the helper
    returns, so a query issued right after does not run ahead of it."""
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    pending: asyncio.Future = hass.loop.create_future()
    # monkeypatch.setattr (not a raw assignment) so this is reverted before
    # the recorder's own shutdown/teardown runs at the end of the test -
    # otherwise the recorder's real close sequence could call this same
    # method and hang or race against a future tied to a loop that's
    # about to be torn down.
    monkeypatch.setattr(instance, "async_get_commit_future", lambda: pending)

    task = hass.async_create_task(job._wait_for_recorder_commit())

    _, still_pending = await asyncio.wait({task}, timeout=0.05)
    assert task in still_pending

    pending.set_result(None)
    await asyncio.wait_for(task, timeout=2)


async def test_wait_for_recorder_commit_proceeds_after_timeout(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """If the recorder's write queue never catches up (e.g. a genuinely
    stuck recorder thread), the helper must still return rather than hang
    a resolve forever."""
    monkeypatch.setattr(lck, "RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS", 0.05)
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    never_resolved: asyncio.Future = hass.loop.create_future()
    monkeypatch.setattr(instance, "async_get_commit_future", lambda: never_resolved)

    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)
    never_resolved.cancel()


async def test_wait_for_recorder_commit_coalesces_rapid_calls(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Two calls in quick succession must only genuinely check the recorder
    once - see RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS. Without this, a
    sequential loop of many per-entity queries (a large install's boot
    pass, or a periodic sweep over many unconfirmed entities) forces one
    early recorder commit per query instead of the recorder's own
    efficient batched commit behaviour - field-observed to stall real
    writes for hours on a busy install."""
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    calls = {"n": 0}

    def _counting_get_commit_future():
        calls["n"] += 1
        return None

    monkeypatch.setattr(
        instance, "async_get_commit_future", _counting_get_commit_future
    )

    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)
    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)

    assert calls["n"] == 1


async def test_wait_for_recorder_commit_rechecks_after_cooldown(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Once the coalescing cooldown has elapsed, the helper must genuinely
    check the recorder again rather than skipping forever."""
    monkeypatch.setattr(lck, "RECORDER_COMMIT_SYNC_MIN_INTERVAL_SECONDS", 0)
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    calls = {"n": 0}

    def _counting_get_commit_future():
        calls["n"] += 1
        return None

    monkeypatch.setattr(
        instance, "async_get_commit_future", _counting_get_commit_future
    )

    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)
    await asyncio.wait_for(job._wait_for_recorder_commit(), timeout=2)

    assert calls["n"] == 2


async def test_boot_pass_calls_wait_for_recorder_commit(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Wiring check: the boot pass must actually call the helper (via
    _iter_bulk_batches) rather than just having it defined and unused."""
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)
    calls = {"n": 0}

    async def _counting_wait() -> None:
        calls["n"] += 1

    monkeypatch.setattr(job, "_wait_for_recorder_commit", _counting_wait)

    await job._async_run_impl()

    assert calls["n"] >= 1
