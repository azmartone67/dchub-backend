"""/dchub grid|site|deal handlers match their endpoints' REAL contracts (2026-07-31).

Sibling of test_slack_search_facility_rows.py — the same class audited across
the remaining subcommands, each of which mis-read its endpoint:

  grid  — called /api/v1/iso/<iso>/snapshot, whose payload carries NO
          metrics/demand_mw at ANY tier (ungated = heartbeat/dcpi/pipeline/
          facilities; teaser-gated `metrics` is a CTA STRING that crashed
          `.get()`). Rendered "Demand: — MW / Renewable mix: 0.0%" plus a
          link to /grids/<iso>, which 404s (the real page is /grid/<iso>),
          and its error copy suggested `hydroquebec`, which the correct
          endpoint does not serve. Now: /api/v1/grid/intelligence/<ISO>
          (the surface the /grid pages render via _fetch_live), demand
          string-coercion, mix percentages computed from MW.
  site  — /api/site-score is auth-required (anonymous self-calls NEVER got a
          score) and its field is `overall_score` — the handler read
          score/composite_score and rendered "?/100".
  deal  — get_deals has NO operator= param (buyer=/seller= only, ANDed), so
          the filter was silently ignored and the newest GLOBAL deals
          rendered under "involving <operator>"; anon masking made every
          value "$?M"; a worker 503 envelope rendered as "No recent deals".

Cross-cutting: _call_dchub sends X-Internal-Key (DCHUB_INTERNAL_KEY,
DCHUB_SYNC_KEY fallback — the #2018/#2025 loopback pattern) and a 'dchub-'
User-Agent (in-route internal bypass on grid/intelligence + self-traffic
analytics bucketing).

CI-SAFETY: no DATABASE_URL, no network — _call_dchub (or urlopen) is
monkeypatched; the module imports directly (never via main).
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


def _cmd(client, mod, text, payload):
    calls = []

    def fake_call(path, params=None, timeout=8):
        calls.append((path, dict(params or {})))
        return payload

    orig = mod._call_dchub
    mod._call_dchub = fake_call
    try:
        r = client.post("/api/v1/slack/command", data={"text": text})
    finally:
        mod._call_dchub = orig
    return r.get_json(), calls


# ── _call_dchub: internal key + UA ───────────────────────────────────

def test_call_dchub_sends_internal_key_and_dchub_ua(mod, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "ik-test-123")
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["ikey"] = req.get_header("X-internal-key")
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    out = mod._call_dchub("/api/v1/deals", {"limit": 100})
    assert out == {"ok": True}
    assert seen["ikey"] == "ik-test-123"
    assert (seen["ua"] or "").startswith("dchub-")


def test_call_dchub_omits_header_without_key(mod, monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("DCHUB_SYNC_KEY", raising=False)
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{}'

    def fake_urlopen(req, timeout=None):
        seen["ikey"] = req.get_header("X-internal-key")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod._call_dchub("/x")
    assert seen["ikey"] is None


# ── grid ─────────────────────────────────────────────────────────────

GRID_FULL = {
    "region": "PJM", "rto_code": "PJM",
    "demand_mw": "88,907",           # STRING shape EIA emits — must coerce
    "generation_mix": {
        "NG": {"mw": 40000}, "NUC": {"mw": 30000}, "WND": {"mw": 10000},
        "COL": {"mw": 8907},
    },
}


def test_grid_calls_grid_intelligence_not_iso_snapshot(client, mod):
    body, calls = _cmd(client, mod, "grid pjm", GRID_FULL)
    assert len(calls) == 1
    assert calls[0][0] == "/api/v1/grid/intelligence/PJM"
    assert "snapshot" not in calls[0][0]
    text = body["text"]
    assert "88,907 MW" in text
    assert "gas 45%" in text and "nuclear 34%" in text
    assert "0.0%" not in text                      # the old always-zero render
    assert "https://dchub.cloud/grid/pjm" in text  # /grid/, the page that exists
    assert "/grids/" not in text                   # the 404 link


def test_grid_teaser_or_empty_payload_never_crashes(client, mod):
    # Anon-gated shape: gen mix redacted to a CTA STRING, no demand.
    body, _ = _cmd(client, mod, "grid pjm",
                   {"generation_mix": "<gated: identified-tier or higher>"})
    assert body["response_type"] == "ephemeral"
    assert "hydroquebec" not in body["text"]       # not served by this endpoint
    assert "nyiso" in body["text"]                 # real supported set

    # Gated-but-headline shape: numeric demand survives, mix skipped.
    body, _ = _cmd(client, mod, "grid pjm",
                   {"demand_mw": 91000,
                    "generation_mix": "<gated: identified-tier or higher>"})
    assert body["response_type"] == "in_channel"
    assert "91,000 MW" in body["text"]
    assert "Mix:" not in body["text"]


# ── site ─────────────────────────────────────────────────────────────

def test_site_reads_overall_score(client, mod):
    body, calls = _cmd(client, mod, "site 39.0,-77.5",
                       {"success": True, "overall_score": 82,
                        "interpretation": "Excellent site"})
    assert calls[0][0] == "/api/site-score"
    assert "*82/100*" in body["text"]
    assert "Excellent site" in body["text"]
    assert "?/100" not in body["text"]


def test_site_error_envelope_is_unavailability_not_bad_location(client, mod):
    body, _ = _cmd(client, mod, "site 39.0,-77.5",
                   {"error": "plan_required", "success": False})
    assert body["response_type"] == "ephemeral"
    assert "temporarily unavailable" in body["text"]
    assert "?/100" not in body["text"]


# ── deal ─────────────────────────────────────────────────────────────

DEALS = {
    "success": True,
    "data": [
        {"buyer": "Blackstone", "seller": "QTS Realty", "value": 10000.0,
         "value_display": "$10.0B", "date": "2026-06-01"},
        {"buyer": "Equinix", "seller": "MainOne", "value": 320.0,
         "value_display": None, "date": "2026-05-12"},
        {"buyer": "DigitalBridge", "seller": "Switch", "value": None,
         "value_display": None, "date": None, "year": 2026},
    ],
}


def test_deal_filters_operator_on_either_side_client_side(client, mod):
    body, calls = _cmd(client, mod, "deal equinix", DEALS)
    path, params = calls[0]
    assert path == "/api/v1/deals"
    assert "operator" not in params        # the param get_deals never had
    text = body["text"]
    assert "Equinix" in text and "MainOne" in text
    assert "Blackstone" not in text        # unrelated rows no longer leak in
    assert "$320M" in text                 # value = USD millions, no value_display


def test_deal_renders_masked_and_missing_values_honestly(client, mod):
    body, _ = _cmd(client, mod, "deal switch", DEALS)
    text = body["text"]
    assert "undisclosed" in text           # value None → never "$?M"/"$NoneM"
    assert "$?M" not in text and "None" not in text
    assert "2026" in text                  # year fallback when date is null


def test_deal_error_vs_no_match_are_distinct(client, mod):
    body, _ = _cmd(client, mod, "deal equinix", {"error": "URLError: timeout"})
    assert body["response_type"] == "ephemeral"
    assert "temporarily unavailable" in body["text"]
    assert "No recent deals" not in body["text"]

    body, _ = _cmd(client, mod, "deal zzz-no-such-operator", DEALS)
    assert body["response_type"] == "ephemeral"
    assert "No recent deals" in body["text"]


def test_deal_without_args_lists_newest_globally(client, mod):
    body, _ = _cmd(client, mod, "deal", DEALS)
    assert body["response_type"] == "in_channel"
    assert "Recent data center M&A" in body["text"]
    assert "Blackstone" in body["text"]
