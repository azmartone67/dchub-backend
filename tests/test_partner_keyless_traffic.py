"""Keyless traffic from a declared partner egress is counted, and the pay links
served to it are attributable to the partner (2026-09-21).

WHY. A hosted catalogue (partner_egress.py) proxies every keyless customer
through one address. Its keyless volume was recorded nowhere: the limiter
buckets live in memory, and the usage tracker records keyed requests only. And
the walls served to that address carried nothing that could tie a checkout to
the partner. A wholesale conversation with the partner needs both numbers.

These tests drive the real pieces: the real limiter's classification, the
tracker's real before/after_request hooks (with the real limiter registered in
front of them, as main.py registers it), and the real paywall builder. The SQL
side (the rollup, the ref table, the read) runs against Postgres in
tests/test_usage_tracker_self_serve_sql.py.
"""
import json
import pathlib
import sys

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import partner_egress  # noqa: E402
import rate_limiter  # noqa: E402
import routes.api_usage_tracker as tracker  # noqa: E402
import routes.partner_attribution as pa  # noqa: E402

PARTNER, PARTNER_IP = "anythingmcp/", "104.248.242.235"
UNDECLARED_IP = "203.0.113.7"             # TEST-NET-3
NEIGHBOUR_IP = "104.248.242.236"
MOVED_IP = "198.51.100.23"                # TEST-NET-2
LIVE_KEY = "dch_live_" + "a" * 32
INTERNAL = "partner-traffic-test-internal-key"
ADMIN = "partner-traffic-test-admin-key"
UA = "node"
CODE = "DCM-PTNR1"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for var in ("DCHUB_PARTNER_EGRESS", "DATABASE_URL", "NEON_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    rate_limiter._buckets.clear()
    rate_limiter._log_budget.clear()
    tracker._drain_partner()
    tracker._drain_buffer()
    pa._drain()
    yield
    rate_limiter._buckets.clear()
    rate_limiter._log_budget.clear()
    tracker._drain_partner()
    tracker._drain_buffer()
    pa._drain()


def _headers(ip, extra=None):
    h = {"User-Agent": UA, "CF-Connecting-IP": ip}
    h.update(extra or {})
    return h


# ── 1. "keyless from the partner" is the limiter's own classification ───────

CASES = [
    (PARTNER_IP, None, PARTNER),
    (PARTNER_IP, {"X-API-Key": LIVE_KEY}, None),
    (PARTNER_IP, {"Authorization": "Bearer " + LIVE_KEY}, None),
    (PARTNER_IP, {"X-Internal-Key": INTERNAL}, None),
    (UNDECLARED_IP, None, None),
    (NEIGHBOUR_IP, None, None),
]


@pytest.mark.parametrize("ip,extra,expected", CASES, ids=[
    "keyless-from-egress", "x-api-key", "bearer", "internal", "undeclared", "neighbour"])
def test_partner_of_request_is_exactly_the_partner_bucket(ip, extra, expected):
    with flask.Flask(__name__).test_request_context(
            "/api/v1/pipeline", headers=_headers(ip, extra)):
        assert rate_limiter.partner_of_request() == expected
        _, tier = rate_limiter._get_key_and_tier()
        assert (tier == "partner") == (expected is not None)


def test_a_moved_egress_moves_the_classification(monkeypatch):
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", json.dumps({PARTNER: [MOVED_IP]}))
    app = flask.Flask(__name__)
    with app.test_request_context("/api/x", headers=_headers(MOVED_IP)):
        assert rate_limiter.partner_of_request() == PARTNER
    with app.test_request_context("/api/x", headers=_headers(PARTNER_IP)):
        assert rate_limiter.partner_of_request() is None
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", "{}")          # the kill switch
    with app.test_request_context("/api/x", headers=_headers(MOVED_IP)):
        assert rate_limiter.partner_of_request() is None


# ── 2. the tracker counts it, through its real hooks ─────────────────────────

@pytest.fixture
def client(monkeypatch):
    """The limiter in front of the tracker, registered in main.py's order."""
    monkeypatch.setattr(tracker, "_ensure_schema", lambda: None)
    monkeypatch.setattr(tracker, "_ensure_flusher_running", lambda: None)
    app = flask.Flask("partner-traffic-test")
    app.add_url_rule("/api/v1/pipeline", "pipeline", lambda: ("walled", 403))
    app.add_url_rule("/api/energy/prices/<state>", "prices", lambda state: "ok")
    app.before_request(rate_limiter.rate_limit_before)
    app.after_request(rate_limiter.rate_limit_after)
    tracker.install_tracker(app)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"   # as Railway's proxy shows it; loopback is exempt
    return c


def _counts():
    with tracker._PARTNER_LOCK:
        return {k[1:]: n for k, n in tracker._PARTNER_COUNTS.items()}


def test_keyless_partner_requests_are_counted_per_route_template_and_status(client):
    for path, status in (("/api/energy/prices/TX", 200), ("/api/energy/prices/TX", 200),
                         ("/api/energy/prices/CA", 200), ("/api/v1/pipeline", 403),
                         ("/api/no/such/route", 404)):
        assert client.get(path, headers=_headers(PARTNER_IP)).status_code == status
    assert _counts() == {
        (PARTNER, "GET", "/api/energy/prices/<state>", 200): 3,
        (PARTNER, "GET", "/api/v1/pipeline", 403): 1,
        (PARTNER, "GET", tracker.UNMATCHED_RULE, 404): 1,
    }


def test_only_the_partners_keyless_requests_are_counted(client):
    client.get("/api/energy/prices/TX", headers=_headers(UNDECLARED_IP))
    client.get("/api/energy/prices/TX", headers=_headers(NEIGHBOUR_IP))
    client.get("/api/energy/prices/TX", headers=_headers(PARTNER_IP, {"X-API-Key": LIVE_KEY}))
    client.get("/api/energy/prices/TX", headers=_headers(PARTNER_IP, {"X-Internal-Key": INTERNAL}))
    assert _counts() == {}
    # the keyed one went where keyed requests always went, and only there
    with tracker._BUFFER_LOCK:
        assert [e["key_prefix"] for e in tracker._BUFFER] == [
            LIVE_KEY[:tracker.STORED_PREFIX_LEN], "internal"]


def test_the_limiters_429s_are_counted_too(client):
    """A 429 returned by a before_request hook skips the tracker's own
    before_request, and is still the partner's volume."""
    rpm = rate_limiter.LIMITS["partner"]["rpm"]
    codes = [client.get("/api/energy/prices/TX", headers=_headers(PARTNER_IP)).status_code
             for _ in range(rpm + 2)]
    assert codes.count(429) == 2
    assert _counts() == {(PARTNER, "GET", "/api/energy/prices/<state>", 200): rpm,
                         (PARTNER, "GET", "/api/energy/prices/<state>", 429): 2}


def test_a_skipped_path_is_not_counted(client):
    client.get("/api/health", headers=_headers(PARTNER_IP))
    assert _counts() == {}


def test_without_a_database_the_counts_are_kept_for_the_next_flush(client, monkeypatch):
    monkeypatch.setattr(tracker, "_pg_conn", lambda: None)
    client.get("/api/energy/prices/TX", headers=_headers(PARTNER_IP))
    before = _counts()
    out = tracker._flush_partner()
    assert out["partner_keyless_rows"] == 0 and out["partner_keyless_skipped"] == "no_db"
    assert _counts() == before == {(PARTNER, "GET", "/api/energy/prices/<state>", 200): 1}


def test_the_flusher_thread_flushes_the_partner_counts_every_pass(monkeypatch):
    """_flush_loop is what runs in production: drive it for one pass."""
    calls = []
    monkeypatch.setattr(tracker, "_flush", lambda: calls.append("keyed") or 1 / 0)
    monkeypatch.setattr(tracker, "_flush_partner", lambda: calls.append("partner"))
    sleeps = []

    def _sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 1:
            raise StopIteration        # end the endless loop after one pass
    import time as _time
    import types
    monkeypatch.setattr(tracker, "time", types.SimpleNamespace(sleep=_sleep, time=_time.time))
    with pytest.raises(StopIteration):
        tracker._flush_loop()
    # the keyed flush raising did not skip the partner flush
    assert calls == ["keyed", "partner"]


def test_the_manual_flush_flushes_the_partner_counts_too(monkeypatch):
    monkeypatch.setattr(tracker, "_flush", lambda: {"flushed": 0})
    monkeypatch.setattr(tracker, "_flush_partner", lambda: {"partner_keyless_rows": 7})
    app = flask.Flask("partner-traffic-flush-test")
    app.register_blueprint(tracker.api_usage_tracker_bp)
    r = app.test_client().post("/api/v1/admin/usage-tracker/flush",
                               headers={"X-Admin-Key": ADMIN})
    assert r.status_code == 200
    assert r.get_json()["partner"] == {"partner_keyless_rows": 7}


def test_a_full_buffer_counts_what_it_drops(monkeypatch):
    monkeypatch.setattr(tracker, "_PARTNER_MAX_KEYS", 1)
    dropped = tracker._PARTNER_DROPPED[0]
    tracker._track_partner(PARTNER, "GET", "/a", 200)
    tracker._track_partner(PARTNER, "GET", "/a", 200)      # same key: counted
    tracker._track_partner(PARTNER, "GET", "/b", 200)      # new key: dropped
    assert _counts() == {(PARTNER, "GET", "/a", 200): 2}
    assert tracker._PARTNER_DROPPED[0] == dropped + 1


# ── 3. the walls served to it carry a ref recorded to the partner ────────────

def _wall(monkeypatch, ip, extra=None, path="/api/v1/pipeline"):
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import utils.paywall_response as pr
    monkeypatch.setattr(pair_code, "get_or_create_code",
                        lambda *a, **k: {"code": CODE, "expires_at": None})
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id", lambda **k: "anon:test")
    with flask.Flask(__name__).test_request_context(path, headers=_headers(ip, extra)):
        return pr.build_paywall_response(tool_name="pipeline", user_id=None)


def _pricing_links(body):
    text = json.dumps(body)
    return [t.split('"')[0].split(")")[0] for t in text.split("https://dchub.cloud/pricing")[1:]]


def test_a_partner_wall_carries_its_pair_code_on_every_pricing_link(monkeypatch):
    body = _wall(monkeypatch, PARTNER_IP)
    assert body["upgrade_url"].endswith("&ref=" + CODE)
    assert body["pricing_url"] == "https://dchub.cloud/pricing?ref=" + CODE
    links = _pricing_links(body)
    assert links and all("ref=" + CODE in link for link in links), links
    with pa._BUF_LOCK:
        assert {r: row[:4] for r, row in pa._BUF.items()} == {
            CODE: [PARTNER, "pair_code", "/api/v1/pipeline", 1]}


@pytest.mark.parametrize("ip,extra", [
    (UNDECLARED_IP, None), (NEIGHBOUR_IP, None), (PARTNER_IP, {"X-API-Key": LIVE_KEY}),
], ids=["undeclared", "neighbour", "keyed-from-egress"])
def test_every_other_wall_is_exactly_what_it_was(monkeypatch, ip, extra):
    body = _wall(monkeypatch, ip, extra)
    monkeypatch.setattr(pa, "attribute_wall", lambda body, *a, **k: body)
    assert body == _wall(monkeypatch, ip, extra)
    assert "pricing_url" not in body and "ref=" not in body["upgrade_url"]
    with pa._BUF_LOCK:
        assert pa._BUF == {}


def test_the_rest_403_wall_serves_the_ref_on_pricing_url(monkeypatch):
    """_rich_gate_response starts from a bare pricing_url; the partner's ref'd one
    must be the one that goes out."""
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import api_tier_gating
    monkeypatch.setattr(pair_code, "get_or_create_code",
                        lambda *a, **k: {"code": CODE, "expires_at": None})
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id", lambda **k: "anon:test")
    with flask.Flask(__name__).test_request_context(
            "/api/v1/pipeline", headers=_headers(PARTNER_IP)):
        body = api_tier_gating._rich_gate_response(path="/api/v1/pipeline",
                                                   min_plan="identified")
    assert body["pricing_url"] == "https://dchub.cloud/pricing?ref=" + CODE
    assert body["upgrade_url"].endswith("&ref=" + CODE)


def test_no_pair_code_means_no_ref_and_no_record(monkeypatch):
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import utils.paywall_response as pr
    monkeypatch.setattr(pair_code, "get_or_create_code", lambda *a, **k: None)
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id", lambda **k: "anon:test")
    with flask.Flask(__name__).test_request_context(
            "/api/v1/pipeline", headers=_headers(PARTNER_IP)):
        body = pr.build_paywall_response(tool_name="pipeline", user_id=None)
    assert "ref=" not in body["upgrade_url"] and "pricing_url" not in body
    assert pa.pending() == 0


class _BrokenConn:
    def cursor(self):
        raise RuntimeError("database went away")

    def rollback(self):
        pass


def test_a_ref_flush_that_cannot_write_keeps_every_ref():
    pa.note_offer_ref(CODE, PARTNER, "pair_code", "/p")
    pa.note_offer_ref(CODE, PARTNER, "pair_code", "/p")
    assert pa.flush(None) == {"refs": 0, "skipped": "no_db"}
    out = pa.flush(_BrokenConn())
    assert out["refs"] == 0 and "database went away" in out["error"]
    with pa._BUF_LOCK:
        assert pa._BUF[CODE][:4] == [PARTNER, "pair_code", "/p", 2]


def test_a_ref_outside_the_checkout_charset_is_never_recorded():
    assert pa.note_offer_ref("DCM-1 x", PARTNER, "pair_code", "/p") is False
    assert pa.note_offer_ref("DCM-1", "", "pair_code", "/p") is False
    assert pa.pending() == 0


# ── 4. the read is admin-only ────────────────────────────────────────────────

@pytest.fixture
def admin_client():
    app = flask.Flask("partner-traffic-admin-test")
    app.register_blueprint(tracker.api_usage_tracker_bp)
    return app.test_client()


def test_the_partner_traffic_read_needs_the_admin_key(admin_client):
    r = admin_client.get("/api/v1/admin/usage-tracker/partner-traffic")
    assert r.status_code == 401
    r = admin_client.get("/api/v1/admin/usage-tracker/partner-traffic",
                         headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


def test_without_a_database_the_read_says_so(admin_client):
    r = admin_client.get("/api/v1/admin/usage-tracker/partner-traffic",
                         headers={"X-Admin-Key": ADMIN})
    assert r.status_code == 503 and r.get_json()["error"] == "no_db"
