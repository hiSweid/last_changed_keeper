"""Tests for the core logic _real_last_changed.

Run: `pytest` with `homeassistant` installed (e.g. via
`pytest-homeassistant-custom-component`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.last_changed_keeper import _real_last_changed

BASE = datetime(2026, 6, 23, 4, 0, tzinfo=timezone.utc)


@dataclass
class FakeState:
    state: str
    last_changed: datetime
    last_updated: datetime


def s(value: str, minutes: int) -> FakeState:
    ts = BASE + timedelta(minutes=minutes)
    return FakeState(value, ts, ts)


def test_simple_real_change_bounded():
    # on -> off (real change), nothing after
    history = [s("on", 40), s("off", 51)]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_skips_restart_recovery():
    # real off at 51, then restarts: unavailable + recovery-off
    history = [
        s("on", 40),
        s("off", 51),          # real last change
        s("unavailable", 114),
        s("off", 116),         # recovery
        s("unavailable", 149),
        s("off", 150),         # recovery (current value)
    ]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed  # not 116 or 150!


def test_unbounded_when_history_exhausted():
    # only recovery-offs in the window, the real change is before it
    history = [
        s("unavailable", 100),
        s("off", 102),
        s("unavailable", 130),
        s("off", 132),
    ]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is False          # no other valid value -> uncertain
    assert ts == s("off", 102).last_changed  # oldest in run (best effort)


def test_state_changed_back_on():
    # off -> on -> off : the current run starts at the last off
    history = [s("off", 10), s("on", 20), s("off", 51)]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_no_valid_states():
    history = [s("unavailable", 10), s("unknown", 20)]
    ts, bounded = _real_last_changed(history, "off")
    assert ts is None
    assert bounded is False


def test_empty_history():
    ts, bounded = _real_last_changed([], "off")
    assert ts is None
    assert bounded is False


def test_unavailable_in_middle_is_skipped():
    # off(real) -> unavailable -> off(recovery): unavailable is skipped,
    # the real off time stays authoritative.
    history = [s("on", 30), s("off", 51), s("unavailable", 120), s("off", 122)]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_many_restart_recoveries_collapse_to_real():
    history = [
        s("on", 40),
        s("off", 51),            # real last change
        s("unavailable", 100), s("off", 101),
        s("unavailable", 140), s("off", 141),
        s("unavailable", 175), s("off", 176),   # current value
    ]
    ts, bounded = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_current_state_on():
    history = [s("off", 10), s("on", 51)]
    ts, bounded = _real_last_changed(history, "on")
    assert bounded is True
    assert ts == s("on", 51).last_changed
