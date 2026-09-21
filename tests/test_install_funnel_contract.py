"""Install funnel (2026-09-20) — probe-excluded visitor -> attempt -> mint.

/api/v1/ops/install-stats reads minted 0 for the twelve /install/<client>
pages. This surface exists to name WHICH rung is empty, and three facts make the
naive version wrong:

  * /api/v1/keys/claim has TWO reuse branches, and both hand back an existing
    key under its ORIGINAL client_name — so a visitor who pressed "mint" can
    leave no install-* row anywhere. The claim handler therefore records an
    attempt on every exit, and these tests drive the real handler through each.
  * page_usage.classify_ua files HeadlessChrome as human, and our own QA fleet
    (Brain-v2-*) as unknown-not-self. The funnel's classifier wraps it.
  * the attempt ledger is new: a 30d attempt count from a 1-day-old ledger is a
    1-day count, so rates and verdicts are withheld over uncovered windows.
"""
import datetime as dt
import importlib
import os
import re
import types

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import routes.install_funnel as f  # noqa: E402
from routes.page_usage import classify_ua  # noqa: E402

NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADLESS = CHROME.replace("Chrome/128.0", "HeadlessChrome/128.0")


def _windows(ledger_start=None):
    w = {"7d": NOW - dt.timedelta(days=7), "30d": NOW - dt.timedelta(days=30)}
    if ledger_start is not None:
        w["since_attempt_ledger"] = ledger_start
    return w


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── registration and roster ───────────────────────────────────────────────

def test_route_is_registered_at_the_public_ops_path():
    app = Flask(__name__)
    f.register_install_funnel(app)
    assert "/api/v1/ops/install-funnel" in {str(r) for r in app.url_map.iter_rules()}


def test_registration_lives_in_the_safe_zone():
    src = _src("main.py")
    idx = src.find("from routes.install_funnel import register_install_funnel")
    assert idx != -1, "install_funnel is not registered in main.py"
    assert src[:idx].count("\n") + 1 < 10000, "late-line registration silently 404s in prod"


def test_roster_is_the_sitemap_roster():
    """A page in the sitemap but not in INSTALL_PAGES would be invisible here."""
    listed = set(re.findall(r"\('/install/([a-z0-9-]+)'", _src("main.py")))
    assert len(listed) >= 12, "sitemap install block not found — guard would be vacuous"
    assert listed == set(f.INSTALL_PAGES)


def test_stems_are_derived_from_install_stats_not_retyped():
    from routes import install_stats as s
    assert f._INSTALL_STEM == s._INSTALL_PREFIX.rstrip("%") == "install-"
    assert f._PROBE_STEM == s._PROBE_PREFIX.rstrip("%") == "install-verify-"
    assert f._NOT_A_PROBE is s._NOT_A_PROBE
    assert s._NOT_A_PROBE in f._MINTS_SQL


def test_every_sql_like_is_bound_never_inlined():
    for name in ("_MINTS_SQL", "_ATTEMPTS_SQL", "_LEDGER_START_SQL"):
        for m in re.finditer(r"LIKE\s+(\S+)", getattr(f, name)):
            assert m.group(1).startswith("%s"), (name, m.group(1))


# ── classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("ua,asn,expect", [
    (CHROME, "COMCAST-7922", "human"),
    (CHROME, "GOOGLE-FIBER", "human"),
    (CHROME, "AMAZON-02", "datacenter"),
    (CHROME, "MICROSOFT-CORP-MSN-AS-BLOCK", "datacenter"),
    (CHROME, "GOOGLE", "datacenter"),
    (CHROME, "CLOUDFLARENET", "human"),   # WARP / Private Relay are people
    (HEADLESS, "COMCAST-7922", "automation"),
    ("Brain-v2-headless/1.0 (DC Hub QA)", None, "self"),
    ("Mozilla/5.0 (compatible; Brain-v2-csp/1.1)", None, "self"),
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", None, "agent"),
    ("python-requests/2.32", None, "agent"),
    ("", None, "unknown"),
])
def test_classify_visit(ua, asn, expect):
    assert f.classify_visit(ua, asn) == expect


def test_the_two_gaps_the_wrapper_closes_are_real_in_the_shared_classifier():
    """If page_usage ever fixes these itself the wrapper is redundant, not wrong."""
    assert classify_ua(HEADLESS) == "human"
    assert classify_ua("Mozilla/5.0 (compatible; Brain-v2-csp/1.1)") != "self"


# ── edge summary ──────────────────────────────────────────────────────────

def _row(path, ua=CHROME, ip="198.51.100.1", asn="COMCAST-7922", status=200,
         hour=NOW - dt.timedelta(hours=3), n=1):
    return {"count": n, "dimensions": {
        "clientRequestPath": path, "userAgent": ua, "clientIP": ip,
        "clientASNDescription": asn, "edgeResponseStatus": status,
        "datetimeHour": hour.strftime("%Y-%m-%dT%H:00:00Z")}}


def test_visitors_are_distinct_ip_ua_pairs_of_human_page_views_only():
    old = NOW - dt.timedelta(days=12)
    rows = [
        _row("/install/claude", n=3),                                  # 1 visitor, 3 views
        _row("/install/claude/", ip="198.51.100.2"),                   # trailing slash, 2nd
        _row("/install/claude", ip="198.51.100.3", status=304),       # revalidation = view
        _row("/install/claude.html", ip="198.51.100.4", status=308),  # redirect != view
        _row("/install/claude", ip="198.51.100.5", hour=old),          # 30d only
        _row("/install/claude", ua=HEADLESS, ip="198.51.100.6"),
        _row("/install/claude", ua="Brain-v2-headless/1.0 (DC Hub QA)", ip="203.0.113.9", n=7),
        _row("/install/claude", ip="20.1.1.1", asn="MICROSOFT-CORP-MSN-AS-BLOCK", n=2),
        _row("/install/grok", ua="ChatGPT-User/1.0", ip="203.0.113.10", n=4),
        _row("/install/wp-login.php", ip="203.0.113.11", n=5),
    ]
    per, facts = f.summarize_edge(rows, _windows(), has_ip=True)
    c7, c30 = per["claude"]["7d"], per["claude"]["30d"]
    assert (c7["visitors"], c30["visitors"]) == (3, 4)
    assert c7["human_requests"] == 5
    assert c7["excluded_requests"] == {"self": 7, "agent": 0, "automation": 1,
                                       "datacenter": 2, "unknown": 0}
    assert per["grok"]["7d"]["visitors"] == 0
    assert per["grok"]["7d"]["excluded_requests"]["agent"] == 4
    assert per["cursor"]["30d"]["visitors"] == 0          # absent page is a real 0
    assert facts["self_requests_seen"] == 7
    assert facts["other_install_paths"] == [("/install/wp-login.php", 5)]


def test_no_client_ip_in_the_query_means_visitors_unavailable_not_zero():
    per, facts = f.summarize_edge([_row("/install/claude")], _windows(), has_ip=False)
    assert per["claude"]["7d"]["visitors"] is None
    assert per["claude"]["7d"]["human_requests"] == 1
    assert "unavailable" in facts["visitor_unit"]


# ── attempts ──────────────────────────────────────────────────────────────

def _att(client, outcome="minted", ip="a", ua="b", ua_class="human",
         at=NOW - dt.timedelta(hours=1), kc=None):
    return (client, at, outcome, ip, ua, ua_class, kc)


def test_attempts_dedupe_exclude_probes_and_keep_reuse_outcomes():
    rows = [
        _att("install-verify-ledger", outcome="ledger_started", ua_class=None),
        _att("install-claude"),
        _att("install-claude"),          # worker retry: same attempter
        _att("install-claude", outcome="reused_unused_cap", ip="c", kc="web-map"),
        _att("install-claude", ua_class="agent"),                # scripted, not a press
        _att("install-verify-funnel-e2e"),                       # our probe
        _att("install-claude", ua_class="self"),                 # our QA UA
    ]
    per, probes = f.summarize_attempts(rows, _windows())
    c = per["claude"]["7d"]
    assert c["mint_attempts"] == 2
    assert c["attempt_requests"] == 4
    assert c["non_browser_requests"] == 1
    assert c["by_outcome"]["minted"] == 2 and c["by_outcome"]["reused_unused_cap"] == 1
    assert probes["requests"] == 2
    assert probes["clients"] == ["install-claude", "install-verify-funnel-e2e"]


# ── verdict and payload ───────────────────────────────────────────────────

def _payload(visitors, attempts, mints, ledger_start=NOW - dt.timedelta(days=40),
             edge_err=None, all_req=50):
    w = _windows(ledger_start)
    edge = {s: {k: {"visitors": 0, "human_requests": 0, "excluded_requests": {}} for k in w}
            for s in f.INSTALL_PAGES}
    att = {s: {k: {"mint_attempts": 0, "by_outcome": {}, "non_browser_requests": 0}
               for k in w} for s in f.INSTALL_PAGES}
    for k in w:
        edge["claude"][k]["visitors"] = visitors
        att["claude"][k]["mint_attempts"] = attempts
    mint = {s: {k: 0 for k in w} for s in f.INSTALL_PAGES}
    for k in w:
        mint["claude"][k] = mints
    return f.build_payload(NOW, w, edge, {"all_audience_requests": all_req}, {}, edge_err,
                           att, {"requests": 0, "clients": []}, mint, [], [("web-x", NOW)],
                           ledger_start, None)


def test_every_funnel_window_is_labelled_probe_excluded():
    p = _payload(0, 0, 0)
    assert p["label"] == "probe-excluded"
    assert all(t["label"] == "probe-excluded" for t in p["funnel"].values())
    assert len(p["pages"]) == 12


def test_zero_visitors_is_a_distribution_verdict():
    assert _payload(0, 0, 0)["funnel"]["30d"]["verdict"]["rung"] == "distribution"


def test_few_visitors_and_no_press_is_still_distribution_with_its_bound():
    v = _payload(5, 0, 0)["funnel"]["30d"]["verdict"]
    assert v["rung"] == "distribution"
    assert v["attempt_rate_upper_95"] == pytest.approx(0.4507, abs=1e-4)


def test_enough_visitors_and_no_press_names_the_page():
    v = _payload(40, 0, 0)["funnel"]["30d"]["verdict"]
    assert v["rung"] == "page" and v["attempt_rate_upper_95"] < 0.10
    assert f.zero_rate_upper_95(29) <= 0.10 < f.zero_rate_upper_95(28)


def test_rates_and_verdicts_are_withheld_where_the_ledger_did_not_exist():
    p = _payload(40, 0, 1, ledger_start=NOW - dt.timedelta(days=1))
    t30 = p["funnel"]["30d"]
    assert t30["attempts_coverage"].startswith("partial")
    assert t30["visitor_to_attempt"] is None and t30["attempt_to_mint"] is None
    assert t30["verdict"]["rung"] == "attempts_not_covered"
    led = p["funnel"]["since_attempt_ledger"]
    assert led["attempts_coverage"] == "full"
    assert led["verdict"]["rung"] == "page"


def test_rates_exist_only_over_a_measured_nonzero_denominator():
    t = _payload(40, 4, 2)["funnel"]["30d"]
    assert (t["visitor_to_attempt"], t["attempt_to_mint"]) == (0.1, 0.5)
    t0 = _payload(0, 0, 0)["funnel"]["30d"]
    assert t0["visitor_to_attempt"] is None


def test_an_edge_failure_is_unmeasured_never_zero():
    p = _payload(0, 0, 0, edge_err="CF_ANALYTICS_READ_TOKEN not set")
    t = p["funnel"]["30d"]
    assert t["visitors"] is None and t["human_requests"] is None
    assert t["verdict"]["rung"] == "unmeasured"
    assert p["control"]["edge"]["instrument"] == "unproven"


def test_an_edge_query_that_sees_no_audience_at_all_is_unproven():
    p = _payload(0, 0, 0, all_req=0)
    assert p["control"]["edge"]["instrument"] == "unproven"
    assert p["funnel"]["30d"]["verdict"]["rung"] == "unmeasured"


def test_endpoint_end_to_end(monkeypatch):
    f._cache.update(at=0.0, payload=None)
    ledger = NOW - dt.timedelta(hours=2)
    monkeypatch.setattr(f, "_db_reads", lambda since: (
        [("install-claude", NOW - dt.timedelta(hours=1))],
        [("install-verify-durability", NOW - dt.timedelta(days=3))],
        [("web-map", NOW - dt.timedelta(days=2))],
        [_att("install-claude", at=NOW - dt.timedelta(hours=1))],
        ledger, None))
    monkeypatch.setattr(f, "_edge_rows", lambda since, until: (
        [_row("/install/claude", hour=dt.datetime.now(dt.timezone.utc)
              .replace(minute=0, second=0, microsecond=0))],
        {"variant": "ip+hour", "has_client_ip": True}, None))
    app = Flask(__name__)
    f.register_install_funnel(app)
    r = app.test_client().get("/api/v1/ops/install-funnel")
    f._cache.update(at=0.0, payload=None)
    assert r.status_code == 200
    d = r.get_json()
    claude = next(p for p in d["pages"] if p["page"] == "/install/claude")
    assert claude["since_attempt_ledger"]["visitors"] == 1
    assert d["probes"]["mints_30d"] == 1 and d["control"]["mints"]["instrument"] == "live"
    assert d["attempt_ledger"]["recorded_since"] == ledger.isoformat()


# ── the recorder ──────────────────────────────────────────────────────────

class _RecCur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))


class _RecConn:
    def __init__(self, log):
        self.log, self.closed = log, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _RecCur(self.log)

    def close(self):
        self.closed = True


def test_recorder_ignores_other_client_names_and_hashes_everything(monkeypatch):
    log, conns = [], []
    monkeypatch.setattr(f, "ensure_attempts_table", lambda: True)
    monkeypatch.setattr(f, "_dsn", lambda: "postgres://stub")
    monkeypatch.setattr(f.psycopg2, "connect",
                        lambda *a, **k: conns.append(_RecConn(log)) or conns[-1])
    assert f.record_install_attempt("web-map", "minted", ip="1.2.3.4") is False
    assert log == []
    key = "dch_live_" + "a" * 32
    ok = f.record_install_attempt("install-claude", "reused_unused_cap", api_key=key,
                                  key_client_name="web-map", ip="198.51.100.7", ua=CHROME)
    assert ok is True and conns[-1].closed
    sql, params = log[-1]
    assert sql.startswith("INSERT INTO install_mint_attempts")
    flat = " ".join(str(p) for p in params)
    for raw in (key, "198.51.100.7", CHROME):
        assert raw not in flat, "a raw identifier reached the attempt ledger"
    assert params[0] == "install-claude" and params[1] == "reused_unused_cap"
    assert params[2] == "web-map" and params[6] == "human" and len(params) == 7


def test_recorder_fails_open(monkeypatch):
    monkeypatch.setattr(f, "ensure_attempts_table", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert f.record_install_attempt("install-claude", "minted") is False


def test_ddl_goes_through_the_blessed_ddl_cursor():
    src = _src("routes/install_funnel.py")
    body = src[src.find("def ensure_attempts_table"):src.find("def record_install_attempt")]
    assert "from db_utils import ddl_cursor" in body


# ── the claim handler records an attempt on EVERY exit ────────────────────

class _ClaimDB:
    """Scripted mcp_dev_keys behind the `_pool` interface."""

    def __init__(self, existing=None, unused=(), fail_insert=False):
        self.existing, self.unused, self.fail_insert = existing, list(unused), fail_insert
        self.inserts = []

    def connection(self):
        db = self

        class _Cur:
            def __init__(self):
                self._one, self._all = None, []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                s = " ".join(sql.split())
                self._one, self._all = None, []
                if s.startswith("SELECT created_at, api_key, tier FROM mcp_dev_keys"):
                    self._one = db.existing
                elif s.startswith("SELECT api_key, tier, COUNT(*) OVER ()"):
                    self._all = db.unused
                elif s.startswith("SELECT COALESCE(MAX("):
                    self._one = (0,)
                elif s.startswith("INSERT INTO mcp_dev_keys"):
                    if db.fail_insert:
                        raise RuntimeError("simulated: storage down")
                    db.inserts.append(params[0])

            def fetchone(self):
                return self._one

            def fetchall(self):
                return self._all

        class _Conn:
            def __enter__(self):
                return types.SimpleNamespace(cursor=_Cur)

            def __exit__(self, *a):
                return False

        return _Conn()


@pytest.fixture
def claim(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub:stub@127.0.0.1:1/stub")
    fme = importlib.import_module("flask_mcp_endpoints")
    assert os.path.realpath(fme.__file__) == os.path.realpath(os.path.join(ROOT, "flask_mcp_endpoints.py"))
    seen = []
    monkeypatch.setattr(f, "record_install_attempt",
                        lambda name, outcome, **kw: seen.append((name, outcome, kw)) or True)
    app = Flask(__name__)
    app.register_blueprint(fme.mcp_bp)
    client = app.test_client()

    def post(db, client_name="install-claude"):
        monkeypatch.setattr(fme, "_pool", db)
        return client.post("/api/v1/keys/claim", json={"client_name": client_name},
                           headers={"CF-Connecting-IP": "198.51.100.7",
                                    "User-Agent": CHROME,
                                    "Referer": "https://dchub.cloud/install/claude"})

    return types.SimpleNamespace(post=post, seen=seen)


EXISTING = "dch_live_" + "e" * 32


def test_same_client_reuse_records_reused(claim):
    r = claim.post(_ClaimDB(existing=(NOW, EXISTING, "identified")))
    assert r.status_code == 200 and r.get_json()["reused"] is True
    assert [(n, o, kw["api_key"], kw["key_client_name"]) for n, o, kw in claim.seen] == [
        ("install-claude", "reused", EXISTING, "install-claude")]


def test_unused_cap_reuse_records_the_other_client_name(claim):
    rows = [(EXISTING, "identified", 3, "web-map")] * 3
    r = claim.post(_ClaimDB(unused=rows))
    assert r.get_json().get("gate") == "unused_key_cap"
    assert [(o, kw["key_client_name"]) for _n, o, kw in claim.seen] == [
        ("reused_unused_cap", "web-map")]


def test_a_fresh_mint_records_minted(claim):
    db = _ClaimDB()
    r = claim.post(db)
    assert r.status_code == 200 and len(db.inserts) == 1
    (n, o, kw), = claim.seen
    assert (n, o, kw["api_key"]) == ("install-claude", "minted", db.inserts[0])
    assert kw["ip"] == "198.51.100.7" and "referer" not in kw


def test_a_storage_failure_records_failed(claim):
    r = claim.post(_ClaimDB(fail_insert=True))
    assert r.status_code == 503
    assert [o for _n, o, _kw in claim.seen] == ["failed"]


def test_the_recorder_is_called_for_every_name_and_decides_itself(claim):
    """The handler does not pre-filter: one predicate, in one place."""
    claim.post(_ClaimDB(), client_name="web-map")
    assert [n for n, _o, _kw in claim.seen] == ["web-map"]
