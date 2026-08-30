"""Tests for routes/brain_intake_common.py — the shared intake plumbing.

This module is mechanical by design (no judgement lives here), so these test
the mechanics future intakes will depend on: the rotation window's coverage
and wrap, and age_hours' "cannot tell" contract.
"""

from datetime import datetime, timedelta, timezone

from routes import brain_intake_common as c


# ── rotate_window ───────────────────────────────────────────────────────

def test_short_list_returns_everything_regardless_of_cycle():
    items = ["a", "b"]
    for cyc in range(5):
        assert c.rotate_window(items, 3, cyc) == ["a", "b"]


def test_window_advances_by_limit_and_wraps():
    items = list("abcdef")
    assert c.rotate_window(items, 2, 0) == ["a", "b"]
    assert c.rotate_window(items, 2, 1) == ["c", "d"]
    assert c.rotate_window(items, 2, 2) == ["e", "f"]
    assert c.rotate_window(items, 2, 3) == ["a", "b"]      # wrapped


def test_wrap_spans_the_end_of_the_list():
    # 5 items, window 2: cycle 2 starts at index 4 and must take the last item
    # plus the first — not a short window.
    items = list("abcde")
    assert c.rotate_window(items, 2, 2) == ["e", "a"]


def test_every_item_is_reached_within_ceil_n_over_limit_cycles():
    # This is the whole reason the function exists: no permanent tail.
    items = ["i%02d" % i for i in range(23)]
    seen = set()
    for cyc in range(8):          # ceil(23/3) = 8
        seen |= set(c.rotate_window(items, 3, cyc))
    assert seen == set(items)


def test_zero_or_negative_limit_returns_nothing():
    assert c.rotate_window(list("abc"), 0, 0) == []
    assert c.rotate_window(list("abc"), -1, 0) == []


def test_empty_list_is_safe():
    assert c.rotate_window([], 3, 0) == []


def test_window_never_returns_more_than_the_limit():
    items = list("abcdefghij")
    for cyc in range(12):
        assert len(c.rotate_window(items, 4, cyc)) == 4


# ── age_hours: the "cannot tell" contract ───────────────────────────────

def test_age_of_a_known_instant():
    t = datetime.now(timezone.utc) - timedelta(hours=3)
    assert 2.9 < c.age_hours(t.isoformat()) < 3.1


def test_naive_timestamps_are_read_as_utc():
    t = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
    assert 1.9 < c.age_hours(t.isoformat()) < 2.1


def test_z_suffix_is_accepted():
    t = datetime.now(timezone.utc) - timedelta(hours=1)
    assert 0.9 < c.age_hours(t.isoformat().replace("+00:00", "Z")) < 1.1


def test_datetime_objects_are_accepted():
    assert c.age_hours(datetime.now(timezone.utc) - timedelta(hours=4)) > 3.9


def test_unparseable_returns_none_rather_than_zero():
    # Kills: returning 0.0 for junk, which every caller would read as "fresh".
    for bad in (None, "", "not-a-date", [], {}):
        assert c.age_hours(bad) is None, bad


# ── cycle_no ────────────────────────────────────────────────────────────

def test_cycle_advances_once_per_ttl_window():
    assert c.cycle_no(3600, now_s=0) == 0
    assert c.cycle_no(3600, now_s=3599) == 0
    assert c.cycle_no(3600, now_s=3600) == 1
    assert c.cycle_no(3600, now_s=7200) == 2


def test_cycle_never_divides_by_zero():
    assert c.cycle_no(0, now_s=123) >= 0
