"""Tests for the retry-delay parser _parse_delays.

Run: `pytest` with `homeassistant` installed (e.g. via
`pytest-homeassistant-custom-component`).
"""
from __future__ import annotations

from custom_components.last_changed_keeper import _parse_delays

DEFAULT = (30, 90, 180)


def test_empty_or_none_returns_default():
    assert _parse_delays("", DEFAULT) == DEFAULT
    assert _parse_delays(None, DEFAULT) == DEFAULT


def test_normal_comma_separated():
    assert _parse_delays("30, 90, 180", DEFAULT) == (30, 90, 180)


def test_semicolons_accepted():
    assert _parse_delays("10;20;30", DEFAULT) == (10, 20, 30)


def test_garbage_falls_back_to_default():
    assert _parse_delays("abc", DEFAULT) == DEFAULT


def test_values_clamped_to_range():
    # 99999 > 3600 is dropped, 60 stays
    assert _parse_delays("60, 99999", DEFAULT) == (60,)


def test_all_out_of_range_returns_default():
    # 0 and 5000 are both invalid -> no value left -> default
    assert _parse_delays("0, 5000", DEFAULT) == DEFAULT


def test_empty_parts_ignored():
    assert _parse_delays("60,,120", DEFAULT) == (60, 120)


def test_list_input_accepted():
    assert _parse_delays([15, 45], DEFAULT) == (15, 45)


def test_whitespace_tolerated():
    assert _parse_delays("  5 ,  10 ", DEFAULT) == (5, 10)
