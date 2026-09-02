"""QA sweep F8 (2026-09-02): the GSC read route can now name what is LOSING.

Measured (findings/3_seo.md, 2026-09-02 00:4xZ): `/api/v1/seo/performance`
aggregated `date >= CURRENT_DATE - days` only, ordered by impressions only,
limit capped at 1000 — the top-1000-by-impressions covered 38% of clicks,
and "pages losing" had to be reconstructed off-box as agg(28d) − agg(14d).

These tests drive the REAL view through a Flask test client with the DB
swapped for a recording cursor, so they check the SQL the route sends and
the JSON it returns — not the presence of substrings in the source.
Every one of them is mutation-verified (PR body).
"""
import datetime as dt

import pytest


class _Cur:
    """Records every execute; answers the coverage query and the grain query
    with canned rows of the right width."""
    def __init__(self, grain_rows):
        self.calls = []
        self._grain_rows = grain_rows
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._last = sql

    def fetchone(self):
        if "MIN(date)" in self._last:
            return (dt.date(2025, 12, 26), dt.date(2026, 8, 29), 247)
        if "CASE WHEN date >=" in self._last:          # site compare totals
            return (312, 50463, 13.8, 349, 63637, 12.6)
        return None

    def fetchall(self):
        return self._grain_rows


class _Conn:
    def __init__(self, cur):
        self._cur = cur
    def cursor(self):
        return self._cur
    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    from flask import Flask
    import routes.gsc_performance as mod
    app = Flask(__name__)
    app.register_blueprint(mod.gsc_perf_bp)
    holder = {}

    def _install(rows):
        cur = _Cur(rows)
        monkeypatch.setattr(mod, "get_db", lambda: _Conn(cur))
        holder["cur"] = cur
        return cur

    c = app.test_client()
    c.install = _install
    c.holder = holder
    return c


def _grain_sql(cur):
    """The query that produced the rows (the last non-coverage execute)."""
    return [s for s, _ in cur.calls if "MIN(date)" not in s][-1]


def _grain_params(cur):
    return [p for s, p in cur.calls if "MIN(date)" not in s][-1]


# ── backward compatibility ───────────────────────────────────────────

def test_default_call_keeps_the_legacy_row_shape_and_window(client):
    cur = client.install([("/facilities/x", 12, 340, 9.5)])
    r = client.get("/api/v1/seo/performance?dimension=page")
    body = r.get_json()
    assert r.status_code == 200 and body["success"] is True
    assert body["window_days"] == 28
    assert body["rows"] == [{"value": "/facilities/x", "clicks": 12,
                             "impressions": 340, "position": 9.5}]
    assert "prior_window" not in body and "compare" not in body
    sql = _grain_sql(cur)
    assert "ORDER BY cur_impr DESC" in sql, sql
    assert "LIMIT %s" in sql and _grain_params(cur)[-1] == 50
    # the window is [today-28, today] — the same dates `>= CURRENT_DATE - 28` returned
    start, end = _grain_params(cur)[1], _grain_params(cur)[2]
    assert (end - start).days == 28
    assert end == dt.datetime.now(dt.timezone.utc).date()


def test_site_grain_default_is_unchanged(client):
    cur = client.install([(dt.date(2026, 8, 29), 40, 7000, 0.0057, 13.8)])
    body = client.get("/api/v1/seo/performance").get_json()
    assert body["dimension"] == "site"
    assert body["rows"][0] == {"date": "2026-08-29", "clicks": 40,
                               "impressions": 7000, "ctr": 0.0057, "position": 13.8}
    assert body["coverage"]["newest"] == "2026-08-29"
    assert "compare" not in body


# ── start / end ──────────────────────────────────────────────────────

def test_explicit_start_end_bound_the_query_on_both_sides(client):
    cur = client.install([])
    body = client.get("/api/v1/seo/performance?dimension=query"
                      "&start=2026-08-02&end=2026-08-29").get_json()
    assert body["window"] == {"start": "2026-08-02", "end": "2026-08-29"}
    assert body["window_days"] == 27
    sql = _grain_sql(cur)
    assert "date >= %s AND date <= %s" in sql
    p = _grain_params(cur)
    assert p[1] == dt.date(2026, 8, 2) and p[2] == dt.date(2026, 8, 29)


def test_end_alone_anchors_a_trailing_window_of_days(client):
    """GSC lags 2–3 days; end=newest must give a FULL window, not a window
    that ends today and is empty for its last three dates."""
    cur = client.install([])
    body = client.get("/api/v1/seo/performance?dimension=query"
                      "&days=7&end=2026-08-29").get_json()
    assert body["window"] == {"start": "2026-08-22", "end": "2026-08-29"}


@pytest.mark.parametrize("qs", ["start=2026-13-01", "end=yesterday",
                                "start=2026-08-29&end=2026-08-01"])
def test_bad_dates_are_400_not_500(client, qs):
    client.install([])
    r = client.get("/api/v1/seo/performance?dimension=query&" + qs)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["success"] is False


# ── order / limit ────────────────────────────────────────────────────

def test_order_clicks_sorts_by_clicks(client):
    cur = client.install([])
    client.get("/api/v1/seo/performance?dimension=page&order=clicks")
    assert "ORDER BY cur_clicks DESC" in _grain_sql(cur)


def test_order_position_is_ascending_nulls_last(client):
    cur = client.install([])
    client.get("/api/v1/seo/performance?dimension=page&order=position")
    assert "ORDER BY cur_pos ASC NULLS LAST" in _grain_sql(cur)


def test_order_is_validated_never_interpolated(client):
    """`order` reaches an f-string, so it MUST be a whitelist lookup."""
    cur = client.install([])
    r = client.get("/api/v1/seo/performance?dimension=page&order=1;DROP")
    assert r.status_code == 400
    assert not cur.calls or all("DROP" not in s for s, _ in cur.calls)


def test_lost_orders_require_compare(client):
    client.install([])
    assert client.get("/api/v1/seo/performance?dimension=page"
                      "&order=lost_clicks").status_code == 400
    assert client.get("/api/v1/seo/performance?dimension=page"
                      "&order=lost_clicks&compare=1").status_code == 200


def test_limit_cap_is_5000(client):
    cur = client.install([])
    client.get("/api/v1/seo/performance?dimension=page&limit=99999")
    assert _grain_params(cur)[-1] == 5000
    cur = client.install([])
    client.get("/api/v1/seo/performance?dimension=page&limit=4321")
    assert _grain_params(cur)[-1] == 4321


# ── compare=1 ────────────────────────────────────────────────────────

def test_compare_windows_are_equal_length_and_adjacent(client):
    client.install([])
    body = client.get("/api/v1/seo/performance?dimension=page&compare=1"
                      "&start=2026-08-16&end=2026-08-29").get_json()
    assert body["window"] == {"start": "2026-08-16", "end": "2026-08-29"}
    assert body["prior_window"] == {"start": "2026-08-02", "end": "2026-08-15"}
    # default-window case: 28 → [today-28, today] vs the 29 dates before it
    body = client.get("/api/v1/seo/performance?dimension=page&compare=1").get_json()
    w, pw = body["window"], body["prior_window"]
    d = lambda s: dt.date.fromisoformat(s)
    assert (d(w["end"]) - d(w["start"])) == (d(pw["end"]) - d(pw["start"]))
    assert d(pw["end"]) == d(w["start"]) - dt.timedelta(days=1)


def test_compare_rows_carry_prior_metrics_from_one_grouped_query(client):
    cur = client.install([("/facilities/equinix-fr5", 0, 5, None, 5, 249, 8.1)])
    body = client.get("/api/v1/seo/performance?dimension=page&compare=1"
                      "&start=2026-08-16&end=2026-08-29").get_json()
    assert body["rows"] == [{"value": "/facilities/equinix-fr5",
                             "clicks": 0, "impressions": 5, "position": None,
                             "prior_clicks": 5, "prior_impressions": 249,
                             "prior_position": 8.1}]
    sql = _grain_sql(cur)
    p = _grain_params(cur)
    # one pass over the UNION of both windows, split by CASE on `start`
    assert sql.count("CASE WHEN date >= %s") >= 3 and "CASE WHEN date < %s" in sql
    assert p[-3] == dt.date(2026, 8, 2) and p[-2] == dt.date(2026, 8, 29), p
    assert all(x == dt.date(2026, 8, 16) for x in p[:10]), p
    assert "position * impressions" in sql   # still impression-weighted


def test_compare_lost_impressions_sorts_by_the_drop(client):
    cur = client.install([])
    client.get("/api/v1/seo/performance?dimension=page&compare=1"
               "&order=lost_impressions")
    assert "ORDER BY (prior_impr - cur_impr) DESC" in _grain_sql(cur)


def test_site_grain_compare_returns_window_totals(client):
    client.install([(dt.date(2026, 8, 29), 40, 7000, 0.0057, 13.8)])
    body = client.get("/api/v1/seo/performance?compare=1&days=7"
                      "&end=2026-08-29").get_json()
    assert body["compare"] == {"clicks": 312, "impressions": 50463,
                               "position": 13.8, "prior_clicks": 349,
                               "prior_impressions": 63637,
                               "prior_position": 12.6}
    assert body["prior_window"] == {"start": "2026-08-14", "end": "2026-08-21"}
    assert body["rows"][0]["date"] == "2026-08-29"   # per-day rows still there


def test_resolve_windows_never_compares_unequal_lengths():
    from routes.gsc_performance import _resolve_windows
    for days in (1, 7, 14, 28, 56, 480):
        s, e, ps, pe = _resolve_windows(days, None, None)
        assert (e - s) == (pe - ps), days
        assert pe == s - dt.timedelta(days=1)
