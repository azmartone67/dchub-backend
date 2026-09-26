"""key_reuse is not comparable across 2026-09-15 — and the payload says so.

#4612 (2026-09-15 06:16Z) and #4616 (19:48Z) stopped handing a keyless caller
its existing trial key back by IP+UA. Every gated anonymous request now mints
its own key, so the same callers produce more keys with fewer calls each:
reused_2plus and returned_next_week fall, keys per IP hash rises. Nothing about
the callers changed. Unmarked, that reads as retention collapsing.

House rule: tests never import main. NO NETWORK, NO DB (a fake connection).
"""
import datetime as dt
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _brk():
    from routes.mcp_retention import SERIES_BREAKS
    (b,) = [b for b in SERIES_BREAKS if b["date"] == "2026-09-15"]
    return b


def test_the_break_is_registered_with_what_and_effect():
    b = _brk()
    assert "#4612" in b["what"] and "#4616" in b["what"]
    assert "reused_2plus" in b["effect"] and "returned_next_week" in b["effect"]
    assert "not comparable" in b["effect"]
    assert "key_reuse" in b["affects"]
    # The cohorts key on IP, not api_key; flagging them would be a false alarm.
    assert "agent_cohort" in b["not_affected"]


@pytest.mark.parametrize("week,pos", [
    ("2026-08-31", "before"),
    ("2026-09-07", "before"),      # ends Mon 09-14 00:00Z, before the break day
    ("2026-09-14", "straddles"),   # contains 2026-09-15
    ("2026-09-21", "after"),
    (dt.date(2026, 9, 14), "straddles"),
    (dt.datetime(2026, 9, 21), "after"),
])
def test_week_positions(week, pos):
    from routes.mcp_retention import series_break_position
    assert series_break_position(week, _brk()) == pos


def test_rows_before_and_straddling_are_marked_after_are_not():
    from routes.mcp_retention import mark_series_breaks
    rows = [{"week": dt.date(2026, 9, 7)}, {"week": dt.date(2026, 9, 14)},
            {"week": dt.date(2026, 9, 21)}]
    mark_series_breaks(rows)
    assert rows[0]["series_break"]["position"] == "before"
    assert rows[1]["series_break"]["position"] == "straddles"
    assert rows[0]["series_break"]["date"] == "2026-09-15"
    assert rows[2]["series_break"] is None


def test_30d_window_crossing():
    from routes.mcp_retention import window_crosses_series_break
    utc = dt.timezone.utc
    assert window_crosses_series_break(dt.datetime(2026, 9, 26, tzinfo=utc), 30) == ["2026-09-15"]
    assert window_crosses_series_break(dt.datetime(2026, 10, 16, 1, tzinfo=utc), 30) == []
    assert window_crosses_series_break(dt.datetime(2026, 9, 14, tzinfo=utc), 30) == []


# ── the endpoint, end to end over a fake connection ──────────────────────────

class _Cur:
    def __init__(self):
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        if "minted_incl_scan" in self.sql and "GROUP BY week" in self.sql:
            return [
                {"week": dt.date(2026, 9, 7), "minted": 200, "reused_2plus": 90,
                 "returned_later": 0, "returned_next_week": 12, "distinct_ips": 150,
                 "minted_incl_scan": 200, "excluded_scan_mints": 0, "excluded_scan_ips": 0},
                {"week": dt.date(2026, 9, 14), "minted": 400, "reused_2plus": 60,
                 "returned_later": 0, "returned_next_week": 5, "distinct_ips": 150,
                 "minted_incl_scan": 400, "excluded_scan_mints": 0, "excluded_scan_ips": 0},
                {"week": dt.date(2026, 9, 21), "minted": 500, "reused_2plus": 20,
                 "returned_later": 0, "returned_next_week": 2, "distinct_ips": 150,
                 "minted_incl_scan": 500, "excluded_scan_mints": 0, "excluded_scan_ips": 0},
            ]
        return []

    def fetchone(self):
        if "cur_wk" in self.sql:
            return {"cur_wk": dt.date(2026, 9, 28)}
        if "minted_30d" in self.sql:
            return {"minted_30d": 1100, "pct_reused_30d": 15.5}
        return {}


class _Conn:
    def cursor(self, *a, **k):
        return _Cur()

    def rollback(self):
        pass

    def close(self):
        pass


def test_endpoint_publishes_series_breaks_and_marks_rows(monkeypatch):
    from flask import Flask
    import routes.mcp_retention as mr
    monkeypatch.setattr(mr, "_conn", lambda: _Conn())
    app = Flask(__name__)
    app.register_blueprint(mr.mcp_retention_bp)
    r = app.test_client().get("/api/v1/mcp/retention?weeks=8")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    j = r.get_json()
    (b,) = j["series_breaks"]
    assert b["date"] == "2026-09-15"
    assert b["what"].startswith("trial-key handback by IP+UA removed (#4612/#4616)")
    assert "not comparable across this date" in b["effect"]
    marks = {row["week"][:16]: row["series_break"] for row in j["key_reuse"]}
    pos = {k: (v or {}).get("position") for k, v in marks.items()}
    assert sorted(pos.values(), key=str) == sorted(["before", "straddles", None], key=str), pos
    assert isinstance(j["summary"]["window_30d_crosses_series_break"], list)


def test_both_pages_show_the_note_and_the_marker():
    for page in ("mcp-dashboard.html", "retention.html"):
        html = open(os.path.join(ROOT, "static", page), encoding="utf-8").read()
        assert 'id="ret-series-break"' in html, page
        i = html.index('id="ret-series-break"')
        note = html[i:html.index("</div>", i)]
        assert "2026-09-15" in note and "not comparable" in note, page
        import re
        assert re.search(r"const brk\s*=\s*k\.series_break\s*\?", html), (
            page + " does not derive the row marker from key_reuse.series_break")
        assert "|| r.week}${brk}</td>" in html.replace("||r.week}", "|| r.week}"), (
            page + " computes the marker but never renders it in the week cell")
