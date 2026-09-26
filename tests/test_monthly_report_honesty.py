"""/reports/monthly (September 2026 audit): four defects on one page.

  · the LLM summary said "1.944 exawatts" beside a tile reading 1,944.9 GW —
    the prompt handed the model the raw MW figure and it mis-converted;
  · "One" (1 facility, 5,000 MW) ranked #2 of the top markets by MW;
  · "Unknown" (98 projects, 52,167 MW) led the construction pipeline;
  · a press-kit quote claimed "Claude and Cursor all citing the platform by
    name in research responses", which nothing in the report measures (its own
    brand_pulse.citation_score_pct read 0.0 that day).

No DB, no network: a fake connection answers every query and records the SQL.
"""
import routes.monthly_trend as mt
import routes.report_narrative as rn


class _Cur:
    def __init__(self, log):
        self.log = log
        self.last = ""

    def execute(self, sql, params=None):
        self.last = " ".join(str(sql).split())
        self.log.append((self.last, params))

    def fetchone(self):
        if "FROM capacity_pipeline" in self.last and "IN %s" in self.last \
                and "GROUP BY" not in self.last:
            return (98, 52166.5)
        return (0, 0, 0, 0)

    def fetchall(self):
        if "FROM facilities" in self.last and "GROUP BY market" in self.last:
            return [("Ashburn", 160, 4296.0)]
        if "FROM capacity_pipeline" in self.last and "GROUP BY market" in self.last:
            return [("CA", 53, 27484.0)]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def rollback(self):
        pass

    def close(self):
        pass


def _report(monkeypatch):
    log = []
    monkeypatch.setattr(mt, "_conn", lambda: _Conn(log))
    return mt._compute_report(2026, 9), log


def _q(log, *needles):
    return [(sql, p) for sql, p in log if all(n in sql for n in needles)]


def test_market_rankings_exclude_placeholder_labels(monkeypatch):
    d, log = _report(monkeypatch)
    for needles in (("FROM facilities", "GROUP BY market"),
                    ("FROM capacity_pipeline", "GROUP BY market")):
        hits = _q(log, *needles)
        assert hits, needles
        for sql, params in hits:
            assert "NOT IN %s" in sql, sql
            assert {"one", "unknown"} <= set(params[0]), params
    assert d["pipeline_unattributed"] == {"projects": 98, "mw": 52166.5}


def test_unattributed_pipeline_is_stated_not_hidden(monkeypatch):
    d, _ = _report(monkeypatch)
    html = mt._render_html(d)
    assert "98 pipeline projects (52,166 MW) with no market recorded" in html


def test_press_kit_claims_no_unmeasured_citations():
    base = {"month_label": "September 2026", "year": 2026, "month": 9,
            "headline": {}, "deal_flow": {}}
    for ai in ({"tool_calls_month": 21959, "mom_pct": 11.6},
               {"tool_calls_month": 21959, "mom_pct": None}):
        qs = mt._build_press_kit(dict(base, ai_traffic=ai))["quotables"]
        assert qs, ai
        for q in qs:
            assert "citing" not in q and "by name" not in q, q


def test_narrative_gets_gw_not_raw_mw_and_drops_impossible_units():
    d = {"year": 2026, "month": 9, "month_label": "September 2026",
         "headline": {"total_mw": 1944857.0}}
    prompt = rn._build_monthly_prompt(d)
    assert '"total_power_tracked_gw": 1944.9' in prompt
    assert "1944857" not in prompt
    assert rn._usable("the base now stands at 1.944 exawatts") is None
    assert rn._usable("1.95 million MW across 24,688 facilities")
