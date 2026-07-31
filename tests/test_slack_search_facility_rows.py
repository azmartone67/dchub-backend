"""/dchub search renders FACILITY rows, not market aggregates (2026-07-31).

The defect: the Slack search handler called /api/v1/facilities/by-market,
whose rows are market AGGREGATES — {market, count, total_mw, operator_count}
(canonical contract per #2026). It rendered them as if they were individual
facilities, so every line came out as
"<https://dchub.cloud/facility/None|?> — ?, — MW".

Pinned here:
  1. search calls the facility-level surface /api/v1/facilities with q=
     (matches city AND operator), never the by-market aggregate.
  2. Free-tier rows (id/name/city/state/provider/slug/profile_url — NO
     power_mw) render honestly: linked name via profile_url, provider,
     location. The literal strings "None" and "facility/None" never appear.
  3. Empty result → ephemeral no-results; _call_dchub error envelope
     ({"error": ...}) → ephemeral "temporarily unavailable", not
     "no facilities found".
  4. Slack mrkdwn control chars in data (&, <, >) are escaped.

CI-SAFETY: no DATABASE_URL, no network — _call_dchub is monkeypatched; the
module imports directly (never via main).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def mod():
    pytest.importorskip("flask")
    pytest.importorskip("psycopg2")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ.pop("SLACK_SIGNING_SECRET", None)   # dev mode: signature check passes
    from routes import slack_app as m
    return m


@pytest.fixture()
def client(mod):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_app_bp)
    return app.test_client()


FREE_TIER_ROWS = {
    "success": True,
    "data": [
        {"id": 101, "name": "Ashburn DC-1", "city": "Ashburn", "state": "VA",
         "country": "US", "provider": "Equinix", "slug": "equinix-ashburn-dc-1",
         "profile_url": "https://dchub.cloud/facilities/equinix-ashburn-dc-1",
         "location_display": "Ashburn, VA"},
        {"id": 102, "name": "AT&T <East>", "city": "Ashburn", "state": "VA",
         "country": "US", "provider": "AT&T", "slug": None},
    ],
    "count": 2,
    "total_matching": 171,
    "tier": "free",
}

AGGREGATE_ROWS = {  # the by-market shape the old code mis-rendered (#2026)
    "success": True,
    "data": [{"market": "Ashburn", "count": 171, "total_mw": 3647.0,
              "operator_count": 42}],
}


def _search(client, mod, payload, query="ashburn"):
    calls = []

    def fake_call(path, params=None, timeout=8):
        calls.append((path, dict(params or {})))
        return payload

    orig = mod._call_dchub
    mod._call_dchub = fake_call
    try:
        r = client.post("/api/v1/slack/command", data={"text": f"search {query}"})
    finally:
        mod._call_dchub = orig
    return r.get_json(), calls


def test_search_hits_facility_level_endpoint_not_by_market(client, mod):
    _, calls = _search(client, mod, FREE_TIER_ROWS)
    assert len(calls) == 1
    path, params = calls[0]
    assert path == "/api/v1/facilities"
    assert "by-market" not in path
    assert params.get("q") == "ashburn"


def test_search_renders_facility_rows_honestly(client, mod):
    body, _ = _search(client, mod, FREE_TIER_ROWS)
    text = body["text"]
    assert body["response_type"] == "in_channel"
    assert "None" not in text
    assert "facility/None" not in text
    assert "— MW" not in text
    assert "<https://dchub.cloud/facilities/equinix-ashburn-dc-1|Ashburn DC-1>" in text
    assert "Equinix" in text
    assert "Ashburn, VA" in text
    assert "171" in text            # total_matching surfaces
    # row without profile_url renders as plain text, never a /facility/None link
    assert "AT&amp;T &lt;East&gt;" in text     # mrkdwn-escaped


def test_search_aggregate_shape_never_renders_as_facilities(client, mod):
    # If the aggregate shape ever comes back (endpoint regression), the
    # renderer must not fabricate facility links from it.
    body, _ = _search(client, mod, AGGREGATE_ROWS)
    assert "facility/None" not in body["text"]
    assert "https://dchub.cloud/facility/" not in body["text"]


def test_search_empty_vs_error_are_distinct(client, mod):
    body, _ = _search(client, mod, {"success": True, "data": []})
    assert body["response_type"] == "ephemeral"
    assert "No facilities found" in body["text"]

    body, _ = _search(client, mod, {"error": "URLError: timeout"})
    assert body["response_type"] == "ephemeral"
    assert "temporarily unavailable" in body["text"]
    assert "No facilities found" not in body["text"]


def test_search_without_args_shows_usage(client, mod):
    orig = mod._call_dchub
    mod._call_dchub = lambda *a, **k: pytest.fail("no args must not call the API")
    try:
        r = client.post("/api/v1/slack/command", data={"text": "search"})
    finally:
        mod._call_dchub = orig
    body = r.get_json()
    assert body["response_type"] == "ephemeral"
    assert "Usage:" in body["text"]
