"""routes/webmcp_master_shell.py test suite (2026-07-11, webmcp-lane).

All mocked (no DB, no network, never imports main). Contract under test:
  1. origin-trial token decode — synthetic v3-format token AND the live
     production token string (public in the frontend _headers); expiry math
     + the <30d warn threshold
  2. findings are filed on DECIDED breakage only (pass=False), through the
     canonical writer — probe errors (pass=None) and healthy checks never file
  3. kill switch WEBMCP_SHELL_DISABLE ⇒ 404 (never 5xx); admin gate ⇒ 403
  4. lane pass logic — decided-only aggregation (gauges don't fail a lane)
  5. cron_heartbeat wires the blueprint + the daily 20:xx UTC dispatch
  6. BOUND_API_PATHS mirrors every binding in js/dchub-webmcp.js (drift list
     is non-empty and keyless-shaped)
"""
import base64
import json
import struct

import flask
import pytest

import routes.webmcp_master_shell as wms

# The live token (public — served verbatim in the frontend _headers file).
LIVE_TOKEN = (
    "A9uh2rbUTot8wFVckR+tpw2PuL5zbz/ojv5Dx1Ydo7oCjlZwyQTqovMGeR4umBopBc3WG"
    "IRf6Xv9Ms+H0gGJqw8AAAByeyJvcmlnaW4iOiJodHRwczovL2RjaHViLmNsb3VkOjQ0My"
    "IsImZlYXR1cmUiOiJXZWJNQ1AiLCJleHBpcnkiOjE3OTQ4NzM2MDAsImlzU3ViZG9tYWlu"
    "Ijp0cnVlLCJpc1RoaXJkUGFydHkiOnRydWV9"
)
LIVE_EXPIRY = 1794873600  # pinned in the trial registration


def _synthetic_token(payload: dict) -> str:
    """Build a structurally-valid v3 origin-trial token (unsigned)."""
    body = json.dumps(payload).encode()
    raw = bytes([3]) + b"\x00" * 64 + struct.pack(">I", len(body)) + body
    return base64.b64encode(raw).decode()


# ── 1 · token decode ──────────────────────────────────────────────────

def test_decode_live_token_payload():
    p = wms.decode_origin_trial_token(LIVE_TOKEN)
    assert p is not None
    assert p["feature"] == "WebMCP"
    assert p["origin"] == "https://dchub.cloud:443"
    assert p["expiry"] == LIVE_EXPIRY


def test_decode_synthetic_token():
    p = wms.decode_origin_trial_token(
        _synthetic_token({"origin": "https://x.test:443", "expiry": 123}))
    assert p == {"origin": "https://x.test:443", "expiry": 123}


@pytest.mark.parametrize("bad", ["", None, "not-base64!!!", "aGVsbG8="])
def test_decode_garbage_returns_none(bad):
    assert wms.decode_origin_trial_token(bad) is None


def test_days_remaining_math_and_threshold():
    # 45 days out → healthy; 10 days out → under the 30d warn threshold.
    now = LIVE_EXPIRY - 45 * 86400
    days, iso = wms.token_days_remaining(LIVE_TOKEN, now_ts=now)
    assert round(days) == 45
    assert iso == "2026-11-17"
    assert days >= wms.TOKEN_WARN_DAYS

    days, _ = wms.token_days_remaining(LIVE_TOKEN, now_ts=LIVE_EXPIRY - 10 * 86400)
    assert round(days) == 10
    assert days < wms.TOKEN_WARN_DAYS


def test_days_remaining_undecodable():
    days, detail = wms.token_days_remaining("junk")
    assert days is None
    assert "undecodable" in detail


def test_days_remaining_no_expiry_field():
    days, detail = wms.token_days_remaining(_synthetic_token({"origin": "x"}))
    assert days is None
    assert "expiry" in detail


# ── 2 · findings on breakage only ─────────────────────────────────────

class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCur()

    def close(self):
        pass


def _payload(*checks):
    return {"lanes": [{"lane": "token", "label": "t", "pass": False,
                       "checks": list(checks)}]}


def test_findings_filed_for_decided_failure(monkeypatch):
    filed = []
    monkeypatch.setattr(wms, "_conn", lambda: _FakeConn())
    import routes.brain_findings_writer as bfw
    monkeypatch.setattr(
        bfw, "upsert_brain_finding",
        lambda cur, **kw: filed.append(kw) or "inserted")
    n = wms._file_findings(_payload(
        {"id": "wm_token_expiry", "name": "token expiry", "pass": False,
         "detail": "10 days remaining"}))
    assert n == 1
    assert filed[0]["issue"] == "webmcp_token_broken"
    assert filed[0]["detector"] == "webmcp_master_shell"
    assert "10 days" in filed[0]["detail"]


def test_no_findings_for_unknowns_or_healthy(monkeypatch):
    # pass=None (probe error) and pass=True must NEVER touch the DB.
    monkeypatch.setattr(
        wms, "_conn",
        lambda: pytest.fail("DB opened with nothing decidedly broken"))
    n = wms._file_findings(_payload(
        {"id": "a", "name": "probe", "pass": None, "detail": "timeout"},
        {"id": "b", "name": "ok", "pass": True, "detail": "fine"}))
    assert n == 0


def test_findings_never_raise_without_db(monkeypatch):
    monkeypatch.setattr(wms, "_conn", lambda: None)
    assert wms._file_findings(_payload(
        {"id": "a", "name": "broken", "pass": False, "detail": "x"})) == 0


# ── 3 · kill switch + admin gate ──────────────────────────────────────

@pytest.fixture()
def app(monkeypatch):
    a = flask.Flask(__name__)
    a.register_blueprint(wms.webmcp_master_shell_bp)
    # No probes / DB in route tests.
    # ★2026-09-02 (D5): _tick_cached passes `beat` through — the stub takes it.
    monkeypatch.setattr(wms, "_run_tick", lambda beat=False: {
        "ok": True, "generated_at": "t", "lanes_pass": 0, "lanes_total": 0,
        "lanes": [], "note": "", "findings_filed": 0})
    wms._cache["payload"] = None
    return a


def test_kill_switch_404_not_5xx(app, monkeypatch):
    monkeypatch.setenv("WEBMCP_SHELL_DISABLE", "1")
    with app.test_client() as c:
        assert c.get("/api/v1/admin/webmcp/master-tick").status_code == 404
        assert c.get("/admin/webmcp").status_code == 404


def test_admin_gate(app, monkeypatch):
    monkeypatch.delenv("WEBMCP_SHELL_DISABLE", raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sesame")
    with app.test_client() as c:
        assert c.get("/api/v1/admin/webmcp/master-tick").status_code == 403
        assert c.get("/admin/webmcp?admin_key=wrong").status_code == 403
        ok = c.get("/api/v1/admin/webmcp/master-tick",
                   headers={"X-Admin-Key": "sesame"})
        assert ok.status_code == 200
        assert ok.get_json()["ok"] is True
        html = c.get("/admin/webmcp?admin_key=sesame")
        assert html.status_code == 200
        assert b"WebMCP Master Shell" in html.data


# ── 4 · lane pass logic (decided-only) ────────────────────────────────

def test_lane_pass_ignores_gauges(monkeypatch):
    monkeypatch.setattr(wms, "_conn", lambda: None)
    monkeypatch.setattr(wms, "_file_findings", lambda p: 0)
    monkeypatch.setattr(wms, "_LANES", [
        ("g", "gauges+pass", lambda c: [
            wms._check("a", "gauge", None, "info"),
            wms._check("b", "decided", True, "ok")], "none"),
        ("f", "one failure", lambda c: [
            wms._check("c", "decided", True, "ok"),
            wms._check("d", "decided", False, "broken")], "none"),
        ("u", "all unknown", lambda c: [
            wms._check("e", "gauge", None, "info")], "none"),
    ])
    p = wms._run_tick()
    by = {l["lane"]: l for l in p["lanes"]}
    assert by["g"]["pass"] is True          # gauge does not block
    assert by["f"]["pass"] is False         # one decided failure fails lane
    assert by["u"]["pass"] is False         # nothing decided ⇒ not green
    assert p["lanes_pass"] == 1


def test_lane_crash_is_contained(monkeypatch):
    monkeypatch.setattr(wms, "_conn", lambda: None)
    monkeypatch.setattr(wms, "_file_findings", lambda p: 0)

    def _boom(c):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(wms, "_LANES", [("x", "boom", _boom, "none")])
    p = wms._run_tick()
    assert p["ok"] is True
    assert p["lanes"][0]["checks"][0]["id"] == "x_error"


# ── 5 · cron_heartbeat wiring ─────────────────────────────────────────

def test_heartbeat_dispatch_daily_quiet_hour():
    import datetime as _dt

    import routes.cron_heartbeat as chb
    entries = [e for e in chb._DISPATCH if e[0] == "webmcp_shell_daily"]
    assert len(entries) == 1
    label, url, method, pred = entries[0]
    assert method == "POST"
    assert "/api/v1/admin/webmcp/master-tick" in url
    assert "fresh=1" in url
    assert pred(_dt.datetime(2026, 7, 11, 20, 10)) is True
    assert pred(_dt.datetime(2026, 7, 11, 21, 10)) is False
    assert pred(_dt.datetime(2026, 7, 11, 19, 59)) is False


def test_heartbeat_registers_blueprint():
    import routes.cron_heartbeat as chb
    fns = getattr(chb.cron_heartbeat_bp, "deferred_functions", [])
    names = {getattr(f, "__name__", "") for f in fns}
    assert "_register_webmcp_shell" in names


# ── 6 · drift list shape ──────────────────────────────────────────────

def test_bound_paths_cover_all_page_tool_bindings():
    # One entry per distinct API the frontend binds (7 base+page bindings
    # pre-?v=3 grew to 10 with /markets ×2 + market-dcpi-score).
    assert len(wms.BOUND_API_PATHS) >= 10
    for p in wms.BOUND_API_PATHS:
        assert p.startswith("/api/"), p
        # keyless probes: no auth material may ride in the drift list
        assert "key" not in p.lower(), p
