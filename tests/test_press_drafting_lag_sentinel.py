"""radar: press_drafting_lag must not fabricate a 9999h gap out of a
response-shape mismatch.

check_press_stale_vs_citations() fetches /api/v1/press-releases to find the
newest press row. The live handler (main.py: list_press_releases) returns a
bare JSON array. The detector's parser called d3.get("items") first, which
raises AttributeError on a list before the isinstance(d3, list) fallback on
the same line is ever reached — silently caught, leaving newest_press=None
on every real call. `None` always takes the `lag_h = 9999` sentinel branch,
so this detector could never report a true gap, only the fake one, for as
long as any dchub_cited citation existed. Observed live as a 9999h finding
against /dc-hub-media even though the actual latest press release was one
day old.
"""

import json

import routes.brain_consistency_radar as radar


def _mock_http_get(citation_body, press_body):
    def _fake(url, timeout=5):
        if "ai-citations/history" in url:
            return citation_body, None
        if "press-releases" in url:
            return press_body, None
        return None, None
    return _fake


def test_list_shaped_press_response_is_parsed_not_treated_as_null(monkeypatch):
    """★ The live shape: a bare JSON array. A citation 12h after the newest
    press row is well under the 24h floor — this must read as no finding,
    not a fabricated 9999h sentinel."""
    citations = json.dumps({"history": [
        {"observed_at": "2026-09-24T12:00:00Z", "dchub_cited": True},
    ]})
    press = json.dumps([
        {"id": 1, "title": "x", "slug": "y", "category": "z",
         "date": "2026-09-24", "subheadline": None, "meta_description": None},
    ])
    monkeypatch.setattr(radar, "_http_get", _mock_http_get(citations, press))
    out = radar.check_press_stale_vs_citations()
    assert out == [], f"expected no finding for a 12h gap, got {out}"


def test_a_real_multi_day_gap_still_fires_with_the_true_lag(monkeypatch):
    """A genuine lag must still fire, and with the real hour count — not the
    9999 sentinel — now that the newest press date parses correctly."""
    citations = json.dumps({"history": [
        {"observed_at": "2026-09-24T12:00:00Z", "dchub_cited": True},
    ]})
    press = json.dumps([
        {"id": 1, "title": "x", "slug": "y", "category": "z",
         "date": "2026-09-20", "subheadline": None, "meta_description": None},
    ])
    monkeypatch.setattr(radar, "_http_get", _mock_http_get(citations, press))
    out = radar.check_press_stale_vs_citations()
    assert len(out) == 1
    assert out[0]["issue"] == "press_drafting_lag"
    assert out[0]["lag_hours"] != 9999
    assert out[0]["lag_hours"] == 108  # 4 days, 12h
