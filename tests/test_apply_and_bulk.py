"""Tests for _apply_last_changed and the bulk-query fallback path."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from custom_components.last_changed_keeper import _apply_last_changed

BASE = datetime(2026, 6, 23, 4, 0, tzinfo=UTC)


@dataclass
class FakeStateWithCache:
    last_changed: datetime
    last_updated: datetime
    last_updated_timestamp: float = 0.0
    _cache: dict = field(default_factory=lambda: {"some": "cached_value"})


@dataclass
class FakeStateNoCache:
    last_changed: datetime
    last_updated: datetime


class FrozenState:
    """Simulates a future HA version where last_changed can't be set."""

    def __init__(self, last_changed: datetime) -> None:
        self.last_changed = last_changed

    def __setattr__(self, name, value):
        if name == "last_changed" and "last_changed" in self.__dict__:
            raise AttributeError("can't set attribute")
        super().__setattr__(name, value)


def test_apply_sets_last_changed_and_clears_cache():
    ts = BASE - timedelta(hours=1)
    state = FakeStateWithCache(last_changed=BASE, last_updated=BASE)
    ok = _apply_last_changed(state, ts, also_updated=False)
    assert ok is True
    assert state.last_changed == ts
    assert state.last_updated == BASE  # untouched
    assert state._cache == {}


def test_apply_also_updates_last_updated_when_enabled():
    ts = BASE - timedelta(hours=1)
    state = FakeStateWithCache(last_changed=BASE, last_updated=BASE)
    ok = _apply_last_changed(state, ts, also_updated=True)
    assert ok is True
    assert state.last_updated == ts
    assert state.last_updated_timestamp == ts.timestamp()


def test_apply_degrades_gracefully_without_cache_attr():
    ts = BASE - timedelta(hours=1)
    state = FakeStateNoCache(last_changed=BASE, last_updated=BASE)
    ok = _apply_last_changed(state, ts, also_updated=False)
    assert state.last_changed == ts
    assert ok is False  # no _cache dict to clear -> degraded


def test_apply_returns_false_when_state_rejects_assignment():
    state = FrozenState(last_changed=BASE)
    ok = _apply_last_changed(state, BASE - timedelta(hours=1), also_updated=False)
    assert ok is False
