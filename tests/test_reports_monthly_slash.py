"""/api/v1/reports/monthly/ (trailing slash) is the monthly report's JSON.

Until 2026-09-21 the only rule matching it was comprehensive_report's
"/api/v1/reports/monthly" with strict_slashes=False. #5127 deleted that rule as
the shadowed loser of bare /api/v1/reports/monthly (monthly_trend wins that one,
and its rule was strict), so the slash form had no rule left: production
answered it with a JSON 404 from railway-primary (measured 2026-09-22) while
the bare path answered 200. /upgrade/ broke the same way (#5223).

The slash form now resolves to the bare path's own view. The payload
comprehensive_report used to serve there is still at /api/v1/reports/monthly.json.

The app is built from the two blueprints that own the /api/v1/reports/monthly
family, registered in main.py's order (monthly_trend first). The report builder
and the LLM narrative are stubbed, so no DB and no network. main.py is never
imported (it opens DB pools and registers ~200 blueprints). The booted app's
shadow check is app-contract-gate's.
"""
import os
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import app_contract_gate as gate  # noqa: E402  (stdlib-only at import time)

BARE = "/api/v1/reports/monthly"
SLASH = BARE + "/"
VIEW = "monthly_trend.monthly_json_current"

# A marker only the stubbed monthly_trend report carries.
_REPORT = {"year": 2026, "month": 9, "month_label": "September 2026",
           "_fixture": "monthly_trend._compute_report"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)

    from routes import monthly_trend
    from routes.comprehensive_report import comprehensive_report_bp

    monkeypatch.setattr(monthly_trend, "_compute_report",
                        lambda *a, **k: dict(_REPORT))
    monkeypatch.setattr(monthly_trend, "_attach_narrative_safe", lambda d: d)

    a = flask.Flask(__name__)
    a.register_blueprint(monthly_trend.monthly_trend_bp)
    a.register_blueprint(comprehensive_report_bp)
    a.testing = True
    return a


def _endpoint(app, path):
    return app.url_map.bind("dchub.cloud").match(path, method="GET")[0]


@pytest.mark.parametrize("path", [BARE, SLASH], ids=["bare", "slash"])
def test_both_spellings_resolve_to_the_monthly_trend_view(app, path):
    assert _endpoint(app, path) == VIEW


def test_slash_serves_the_same_report_as_the_bare_path(app):
    client = app.test_client()
    bare = client.get(BARE)
    slash = client.get(SLASH)
    assert bare.status_code == 200, bare.status_code
    # 200, not a 308 to the bare path and not a 404.
    assert slash.status_code == 200, (slash.status_code, slash.get_data()[:200])
    body = slash.get_json()
    assert body == bare.get_json()
    assert body["_fixture"] == _REPORT["_fixture"], body
    assert body["license"]["id"] == "CC-BY-4.0", body.get("license")
    for h in ("Cache-Control", "Access-Control-Allow-Origin", "Link"):
        assert slash.headers.get(h) == bare.headers.get(h), h


@pytest.mark.parametrize("path,view", [
    (BARE + "/2026-08", "monthly_trend.monthly_json_specific"),
    (BARE + "/narrative", "monthly_trend.monthly_narrative_only"),
    (BARE + ".json", "comprehensive_report.monthly_json"),
], ids=["month", "narrative", "dot_json"])
def test_neighbouring_paths_keep_their_own_views(app, path, view):
    assert _endpoint(app, path) == view


def test_the_slash_form_is_not_a_shadowed_duplicate(app):
    assert gate.shadowed(app) == {}
