"""D10: findings parked at deferred_rate_cap carry their age; the radar alarms >7d.

Measured 2026-09-02 00:33Z: /api/v1/brain/finding-routes active=29,
deferred_rate_cap=27; propose stage backlog=29 proposed=0 — green with zero
output, and no lane said for how long. Pure; DB reads are injected.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import brain_finding_router as fr  # noqa: E402

NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.timezone.utc)


def _item(issue, url="dchub://cron/x"):
    return {"issue": issue, "url": url, "detail": "", "count": 1}


def test_age_is_hours_since_first_seen_and_unknown_is_none():
    it = _item("iso_metric_count_zero_24h eu")
    ages = {(it["issue"], it["url"]): NOW - dt.timedelta(days=9)}
    assert fr.deferred_age_h(it, ages, NOW) == 216.0
    assert fr.deferred_age_h(_item("other"), ages, NOW) is None


def test_deferred_findings_carry_age_and_the_over_7d_count():
    # Kills: never stamping the age, or counting every deferral as over 7d.
    old, new = _item("a"), _item("b")
    oc = {("a", old["url"]): "deferred_rate_cap", ("b", new["url"]): "deferred_rate_cap"}
    ages = {("a", old["url"]): NOW - dt.timedelta(days=8),
            ("b", new["url"]): NOW - dt.timedelta(hours=5)}
    out = fr.classify_items([old, new], outcomes=oc, ages=ages, now=NOW)
    by = {e["issue"]: e for e in out["active"]}
    assert by["a"]["deferred_age_h"] == 192.0 and by["b"]["deferred_age_h"] == 5.0
    assert out["deferred_over_7d"] == 1


def test_non_deferred_findings_carry_no_age():
    it = _item("c")
    out = fr.classify_items([it], outcomes={("c", it["url"]): "proposed"}, ages={}, now=NOW)
    assert "deferred_age_h" not in out["active"][0] and out["deferred_over_7d"] == 0


# ── the radar check ──────────────────────────────────────────────────────

def test_the_check_is_registered_in_scan_all():
    # Kills: defining the detector without adding it to the sweep tuple —
    # an unregistered check never runs (util/brain_detector_rule).
    from util import brain_detector_rule as r
    src = open(os.path.join(ROOT, "routes", "brain_consistency_radar.py"),
               encoding="utf-8").read()
    assert "check_findings_deferred_over_7d" in r.registered_checks(src)


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.c = _Cur(rows)

    def cursor(self):
        return self.c

    def close(self):
        pass


def test_the_check_alarms_with_count_and_names_the_cap(monkeypatch):
    import routes.brain_consistency_radar as radar
    rows = [("iso_metric_count_zero_24h eu", "dchub://iso/eu", 9 * 24.0, 40),
            ("slow_loop press", "dchub://press", 8 * 24.0, 12)]
    monkeypatch.setattr(radar, "_db", lambda: _Conn(rows))
    out = radar.check_findings_deferred_over_7d()
    assert len(out) == 1
    f = out[0]
    assert f["issue"] == "findings_deferred_over_7d" and f["count"] == 2
    assert f["count_kind"] == "item_count"
    assert "BRAIN_MAX_LEARN" in f["detail"] and "not raised" in f["detail"]
    assert "oldest 9d" in f["detail"]


def test_the_check_is_quiet_when_nothing_is_parked(monkeypatch):
    import routes.brain_consistency_radar as radar
    monkeypatch.setattr(radar, "_db", lambda: _Conn([]))
    assert radar.check_findings_deferred_over_7d() == []
    monkeypatch.setattr(radar, "_db", lambda: None)
    assert radar.check_findings_deferred_over_7d() == []
