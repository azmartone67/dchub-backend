"""The payer survey died on a TEXT timestamp (2026-09-20).

#4915 taught /api/v1/admin/entitlements/reconcile to read the ACCOUNT surface —
a real improvement: it stopped reporting `never_started` for payers who sign in
and use the map but make no API calls. It added `a.created_at` and
`a.last_login` from `users`, and put both in the loop that calls `.isoformat()`.

`users.created_at` and `users.last_login` are **TEXT** in the DDL
(api_server.py, main.py:14812, static/auto_pilot.py) and are WRITTEN as ISO-8601
strings by utc_iso_z(). So the same day, the endpoint answered:

    {"error":"'str' object has no attribute 'isoformat'","success":false}   500

That is the read-only survey behind the largest open money question on the
board — four payers, $1,148 of $1,853 MRR, provisioned or not. It was down.

★ The GET hid it. The worker's r-origin-5xx passthrough is scoped to non-GET, so
  the GET fell through the stale-KV ladder to "Backend unreachable and no cached
  data available" — which is false, and sends a reader to Railway. POST without
  `confirm=1` runs the SAME survey, provisions nothing, and is a non-GET, so the
  origin's own words survive. That is how this was found.

CI-SAFETY: pure function, no network, no DB.
"""
from datetime import datetime, timezone

import pytest

from routes.entitlement_reconcile import (_TEXT_TS_COLS, _TS_COLS, _iso_row)


def _row(**kw):
    base = {k: None for k in (*_TS_COLS, *_TEXT_TS_COLS)}
    base.update(kw)
    return base


def test_the_bug_a_text_timestamp_no_longer_raises():
    row = _row(last_login="2026-09-20T04:00:00Z",
               account_created_at="2026-08-01T12:00:00Z")
    out = _iso_row(row)
    assert out["last_login"] == "2026-09-20T04:00:00Z"
    assert out["account_created_at"] == "2026-08-01T12:00:00Z"


def test_real_timestamps_are_still_serialized():
    dt = datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)
    out = _iso_row(_row(paid_at=dt, mcp_last_used=dt, rest_last_used=dt))
    for k in _TS_COLS:
        assert out[k] == dt.isoformat(), k
        assert isinstance(out[k], str)


def test_nulls_stay_null():
    out = _iso_row(_row())
    for k in (*_TS_COLS, *_TEXT_TS_COLS):
        assert out[k] is None, k


def test_the_text_columns_survive_a_migration_to_timestamptz():
    """Scoped hasattr, so `users` becoming timestamptz needs no code change —
    while the three genuine timestamp columns stay strict."""
    dt = datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)
    out = _iso_row(_row(last_login=dt, account_created_at=dt))
    assert out["last_login"] == dt.isoformat()
    assert out["account_created_at"] == dt.isoformat()


def test_a_string_in_a_REAL_timestamp_column_still_raises():
    """The load-bearing half. A blanket hasattr() over all five would make this
    pass silently and hide a genuine schema change in mcp_conversions or
    mcp_dev_keys — the two classes must not be defended identically."""
    with pytest.raises(AttributeError):
        _iso_row(_row(paid_at="2026-09-20T04:00:00Z"))


def test_the_two_column_sets_are_disjoint_and_cover_the_query():
    assert not set(_TS_COLS) & set(_TEXT_TS_COLS)
    # every date-ish column the survey SELECTs must be classified in one of them
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "routes", "entitlement_reconcile.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    for col in ("paid_at", "mcp_last_used", "rest_last_used",
                "account_created_at", "last_login"):
        assert f"AS {col}" in src or f"p.{col}" in src or col in src, col
        assert col in (*_TS_COLS, *_TEXT_TS_COLS), f"{col} is classified nowhere"
