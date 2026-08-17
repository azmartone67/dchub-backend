"""db_utils must ADD a SQLite datetime() modifier, never subtract it.

SQLite's `datetime('now', M)` applies M with its own sign: '-3 days' means
three days AGO. _translate_sql rewrote that to

    NOW() - INTERVAL '-3 days'

which is three days into the FUTURE. Unlike the UndefinedFunction class
this module exists to prevent, the inverted form is valid Postgres: it
executes, returns almost nothing, and logs nothing. A caller sees "no
recent rows", not an error.

It stayed hidden because SQLITE_TO_PG_FUNC is consulted first and its
eight entries are written sign-stripped and correct ('-7 days' ->
INTERVAL '7 days'). So the common windows were right, and only intervals
absent from that dict inverted. Measured against the Neon read replica on
2026-08-17, over `announcements`:

    '-7 days'   (in dict)   1,071 rows   correct
    '-3 days'   (generic)       1 row    should be   201
    '-14 days'  (generic)       1 row    should be 2,225

Live callers of the inverted spellings at the time: ai_orchestrator
(-3 days, -14 days), simple_alerts (-1 day) and news_engine (-168 hours).

WHAT THESE GUARDS PIN
  1. A negative modifier moves BACKWARD and a positive one moves FORWARD,
     for every unit — the property, not the eight spellings.
  2. The dict path and the generic path agree, so which spelling you
     happen to write cannot change which direction time runs.
  3. Bare datetime('now') still maps to NOW().

These are pure string/interval assertions plus arithmetic evaluated by
Python's own datetime: CI runs with no DATABASE_URL and must not need
one. Nothing runs at module scope.
"""

import re

import pytest

from db_utils import SQLITE_TO_PG_FUNC, _translate_sql

# (modifier, timedelta-equivalent seconds, direction) — direction is what the
# window must do relative to now.
MODIFIERS = [
    ("-1 day", -86400),
    ("-3 days", -3 * 86400),
    ("-7 days", -7 * 86400),
    ("-14 days", -14 * 86400),
    ("-30 days", -30 * 86400),
    ("-90 days", -90 * 86400),
    ("-365 days", -365 * 86400),
    ("-6 hours", -6 * 3600),
    ("-12 hours", -12 * 3600),
    ("-24 hours", -24 * 3600),
    ("-48 hours", -48 * 3600),
    ("-168 hours", -168 * 3600),
    ("-30 minutes", -30 * 60),
    ("3 days", 3 * 86400),
    ("12 hours", 12 * 3600),
]

_INTERVAL = re.compile(r"NOW\(\)\s*([+-])\s*INTERVAL\s*'([^']+)'", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}


def _translated_offset(modifier):
    """Seconds the translated SQL moves NOW() by. Parsed from the SQL itself."""
    sql, _ = _translate_sql(f"SELECT 1 FROM t WHERE x > datetime('now', '{modifier}')")
    m = _INTERVAL.search(sql)
    assert m, f"no NOW() +/- INTERVAL produced for {modifier!r}; got: {sql}"

    op, body = m.group(1), m.group(2).strip()
    parts = re.fullmatch(r"([+-]?\d+)\s*([a-z]+?)s?", body.strip(), re.IGNORECASE)
    assert parts, f"unparseable interval {body!r} for modifier {modifier!r}"

    qty, unit = int(parts.group(1)), parts.group(2).lower()
    assert unit in _UNIT_SECONDS, f"unknown unit {unit!r} for {modifier!r}"
    seconds = qty * _UNIT_SECONDS[unit]
    return -seconds if op == "-" else seconds


@pytest.mark.parametrize("modifier,expected", MODIFIERS)
def test_modifier_moves_time_the_way_sqlite_would(modifier, expected):
    """'-3 days' must land 3 days back, not 3 days forward."""
    got = _translated_offset(modifier)
    assert got == expected, (
        f"datetime('now', {modifier!r}) translates to a {got:+d}s shift; SQLite "
        f"means {expected:+d}s. A sign flip here does not raise — it returns an "
        f"almost-empty result set and logs nothing.")


@pytest.mark.parametrize("modifier,expected", MODIFIERS)
def test_negative_modifiers_look_backward(modifier, expected):
    """The property, stated separately from the arithmetic."""
    got = _translated_offset(modifier)
    if expected < 0:
        assert got < 0, f"{modifier!r} produced a FUTURE window ({got:+d}s)"
    else:
        assert got > 0, f"{modifier!r} produced a PAST window ({got:+d}s)"


def test_dict_and_generic_paths_agree():
    """Whether a spelling happens to be in SQLITE_TO_PG_FUNC must not change
    which direction time runs."""
    dict_keys = [k for k in SQLITE_TO_PG_FUNC if k.startswith("datetime(")]
    assert dict_keys, "SQLITE_TO_PG_FUNC has no datetime entries — guard scanned nothing"

    checked = 0
    for key in dict_keys:
        m = re.fullmatch(r"datetime\('now',\s*'([^']+)'\)", key)
        if not m:
            continue
        modifier = m.group(1)
        checked += 1
        via_dict = _translated_offset(modifier)
        # Same modifier, a spelling the dict does NOT hold (no space after comma).
        sql, _ = _translate_sql(
            f"SELECT 1 FROM t WHERE x > datetime('now','{modifier}')")
        m2 = _INTERVAL.search(sql)
        assert m2, f"generic path produced no interval for {modifier!r}: {sql}"
        parts = re.fullmatch(r"([+-]?\d+)\s*([a-z]+?)s?", m2.group(2).strip(), re.IGNORECASE)
        qty, unit = int(parts.group(1)), parts.group(2).lower()
        secs = qty * _UNIT_SECONDS[unit]
        via_generic = -secs if m2.group(1) == "-" else secs
        assert via_dict == via_generic, (
            f"{modifier!r}: dict path shifts {via_dict:+d}s but the generic path "
            f"shifts {via_generic:+d}s — the same window depends on whitespace")
    assert checked, "no datetime dict key parsed — guard scanned nothing"


def test_bare_now_still_maps_to_now():
    sql, _ = _translate_sql("SELECT 1 FROM t WHERE x > datetime('now')")
    assert "NOW()" in sql, sql
    assert "datetime" not in sql.lower(), sql


def test_no_datetime_call_survives_translation():
    """Whatever the modifier, nothing datetime()-shaped may reach psycopg2."""
    for modifier, _ in MODIFIERS:
        sql, _ = _translate_sql(
            f"SELECT 1 FROM t WHERE x > datetime('now', '{modifier}')")
        assert not re.search(r"\bdatetime\s*\(", sql, re.IGNORECASE), (
            f"datetime() survived translation for {modifier!r}: {sql}")
