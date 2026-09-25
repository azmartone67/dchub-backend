"""Key-mint scan guard — the parts that need no database (r-mint-scan, 2026-09-24).

Measured 2026-09-24, GET /api/v1/mcp/retention -> key_reuse:

    week        minted  distinct_ips  reused_2plus  returned_next_week
    2026-08-24     195           153           137                  30
    2026-08-31     226           188           187                   9
    2026-09-07     238           208           195                  17
    2026-09-14  11,442           145         9,082                   0

Three guards, each pinned here by behaviour, not by grep:

  * the per-caller mint ceiling in routes/auto_trial.mint_trial_for_request
    (every inline mint door funnels through it) and in POST /api/v1/keys/claim,
    with a real 429 + Retry-After on POST /api/v1/keys/auto-mint;
  * the pure spike decision behind the radar detector check_weekly_mint_spike;
  * check_mint_rate's own decision logic (which scope counts, fail-open).

The SQL itself — which rows count as scan, what the KPIs read — is proven
against a real Postgres in tests/test_mint_scan_guard_sql.py.
"""
import datetime as _dt
import os
from unittest import mock

import pytest

from routes import mint_guard
from routes.mint_guard import check_mint_rate, weekly_mint_spike


@pytest.fixture(autouse=True)
def _enforcing(monkeypatch):
    """The door tests below pin what ENFORCEMENT does. Enforcement is opt-in
    (DCHUB_MINT_RL_MODE=enforce, owner decision 2026-09-24: log-only first);
    the log-only default is pinned at the end of this file."""
    monkeypatch.setenv("DCHUB_MINT_RL_MODE", "enforce")


# ── the spike decision ──────────────────────────────────────────────────────

def test_the_measured_week_is_a_spike():
    hit = weekly_mint_spike(11442, [217, 195, 226, 238])
    assert hit is not None
    assert hit["ratio"] > 40


def test_a_normal_week_is_not():
    assert weekly_mint_spike(238, [217, 195, 226, 160]) is None
    # 4.9x the median is growth, not an alert (threshold is > 5x)
    assert weekly_mint_spike(int(4.9 * 210), [200, 210, 210, 220]) is None


def test_the_floor_keeps_a_tiny_baseline_quiet():
    """3 -> 40 is 13x, and still nothing anyone should be paged about."""
    assert weekly_mint_spike(40, [3, 2, 4, 3]) is None


def test_no_history_is_not_a_spike():
    assert weekly_mint_spike(11442, [0]) is None


def test_the_spike_thresholds_are_env_tunable():
    with mock.patch.dict(os.environ, {"DCHUB_MINT_SPIKE_MULTIPLE": "100"}):
        assert weekly_mint_spike(11442, [217, 195, 226, 238]) is None


# ── check_mint_rate's decision ──────────────────────────────────────────────

class _CountCur:
    """Answers the one count query check_mint_rate issues."""
    def __init__(self, row=None, boom=False):
        self.row, self.boom, self.params = row, boom, None

    def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("db down")
        self.params = params

    def fetchone(self):
        return self.row


def _row(ip_h=0, ip_d=0, ua_h=0, ua_d=0, ra=(100, 200, 300, 400)):
    return (ip_h, ip_d, ua_h, ua_d) + tuple(ra)


def test_under_every_ceiling_mints():
    assert check_mint_rate(_CountCur(_row(9, 29, 59, 499)), "trial",
                           ip_key="h", ua="Grok") is None


def test_the_ip_hour_ceiling_refuses_with_its_retry_after():
    hit = check_mint_rate(_CountCur(_row(ip_h=10, ip_d=10)), "trial",
                          ip_key="h", ua="Grok")
    assert hit and hit["scope"] == "ip" and hit["window"] == "hour"
    assert hit["retry_after"] == 100


def test_the_ceiling_that_clears_last_is_reported():
    """A caller that honours Retry-After must not be refused again: when the
    hour AND day ceilings are both hit, report the day's (later) retry."""
    hit = check_mint_rate(_CountCur(_row(ip_h=10, ip_d=30)), "trial",
                          ip_key="h", ua="Grok")
    assert hit["window"] == "day" and hit["retry_after"] == 200


def test_the_ua_ceiling_refuses():
    hit = check_mint_rate(_CountCur(_row(ua_h=60)), "trial", ip_key="h", ua="Grok")
    assert hit and hit["scope"] == "ua"


def test_a_scope_that_is_not_the_callers_is_never_counted():
    """The MCP gateway's IP is its own egress: count_ip=False must bind NULL
    for the ip (matching no row) AND ignore an ip count even if one came back."""
    cur = _CountCur(_row(ip_h=999, ip_d=999))
    assert check_mint_rate(cur, "trial", ip_key="h", ua="Grok",
                           count_ip=False) is None
    assert cur.params["ip"] is None
    assert cur.params["ua"] == "Grok"


def test_zero_disables_a_ceiling():
    with mock.patch.dict(os.environ, {"DCHUB_MINT_RL_IP_PER_HOUR": "0",
                                      "DCHUB_MINT_RL_IP_PER_DAY": "0"}):
        assert check_mint_rate(_CountCur(_row(ip_h=10**6, ip_d=10**6)), "trial",
                               ip_key="h", ua="Grok") is None


def test_a_broken_counter_fails_open():
    """Minting is the free-tier on-ramp: a DB error must never close it."""
    assert check_mint_rate(_CountCur(boom=True), "trial", ip_key="h", ua="x") is None
    assert check_mint_rate(_CountCur(row=None), "trial", ip_key="h", ua="x") is None


# ── the auto-mint door: mint_trial_for_request, the real function ───────────

class _TrialCur:
    """Stub DB for mint_trial_for_request: a presented-key lookup, the rate
    count, the carry-forward MAX()es and the INSERT."""
    def __init__(self, counts, key_row=None):
        self.counts, self.key_row = counts, key_row
        self.queries, self.inserted, self._last = [], [], None

    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        self.queries.append((q, params))
        if "from auto_trial_keys where api_key = %s" in q:
            self._last = self.key_row
        elif "count(*) filter" in q and "request_ip_hash = %(ip)s" in q:
            self._last = self.counts
        elif q.startswith("select coalesce(max("):
            self._last = (0,)
        elif q.startswith("insert into auto_trial_keys"):
            self.inserted.append(params)
            self._last = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=7),)
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _TrialConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self.remote_addr = "203.0.113.9"


def _mint(counts, *, headers=None, key_row=None, env=None):
    import routes.auto_trial as at
    cur = _TrialCur(counts, key_row=key_row)
    h = {"CF-Connecting-IP": "203.0.113.9", "User-Agent": "Grok/1.0"}
    h.update(headers or {})
    with mock.patch.object(at, "_conn", lambda: _TrialConn(cur)), \
         mock.patch.object(at, "_ensure_schema", lambda c: None), \
         mock.patch.dict(os.environ, env or {}):
        out = at.mint_trial_for_request(_Req(h), "get_market_intel")
    return out, cur


def test_a_caller_over_its_ceiling_is_refused_and_nothing_is_written():
    out, cur = _mint(_row(ip_h=10, ip_d=10))
    assert out["ok"] is False and out["reason"] == "rate_limited", out
    assert out["error"] == "mint_rate_limited"
    assert out["retry_after"] > 0
    assert not cur.inserted, "a refused caller must not get a new row"
    assert "api_key" not in out, "a refused caller is never handed a key"


def test_a_caller_under_its_ceiling_still_mints():
    out, cur = _mint(_row(ip_h=2, ip_d=2))
    assert out["ok"] is True and out["api_key"].startswith("dch_trial_")
    assert len(cur.inserted) == 1


def test_a_caller_presenting_its_own_key_is_never_limited():
    """Handing back the key the caller already holds is not a mint."""
    exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3)
    out, cur = _mint(_row(ip_h=10**6, ip_d=10**6, ua_h=10**6, ua_d=10**6),
                     headers={"X-API-Key": "dch_trial_MINE"},
                     key_row=("dch_trial_MINE", exp, 1, False))
    assert out["ok"] is True and out["api_key"] == "dch_trial_MINE"
    assert not any("count(*) filter" in q for q, _ in cur.queries)


def test_the_gateway_is_limited_by_the_agents_ua_not_its_own_ip():
    """X-Internal-Key = the MCP gateway, whose request IP is its own egress."""
    out, cur = _mint(_row(ip_h=10**6, ip_d=10**6),
                     headers={"X-Internal-Key": "k-int"},
                     env={"DCHUB_INTERNAL_KEY": "k-int"})
    assert out["ok"] is True, out
    count_q = [p for q, p in cur.queries if "count(*) filter" in q]
    assert count_q and count_q[0]["ip"] is None and count_q[0]["ua"] == "Grok/1.0"


def test_a_wrong_internal_key_does_not_buy_the_exemption():
    out, _ = _mint(_row(ip_h=10, ip_d=10),
                   headers={"X-Internal-Key": "guess"},
                   env={"DCHUB_INTERNAL_KEY": "k-int"})
    assert out["reason"] == "rate_limited"


def test_the_endpoint_answers_429_with_retry_after():
    flask = pytest.importorskip("flask")
    import routes.auto_trial as at
    app = flask.Flask(__name__)
    app.register_blueprint(at.auto_trial_bp)
    refused = mint_guard.rate_limited_body(
        {"scope": "ip", "window": "hour", "limit": 10, "count": 10,
         "retry_after": 1234})
    with mock.patch.object(at, "mint_trial_for_request", lambda *a, **k: refused):
        r = app.test_client().post("/api/v1/keys/auto-mint?tool=x")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1234"
    assert r.get_json()["error"] == "mint_rate_limited"

    ok = {"ok": True, "api_key": "dch_trial_x"}
    with mock.patch.object(at, "mint_trial_for_request", lambda *a, **k: ok):
        r = app.test_client().post("/api/v1/keys/auto-mint?tool=x")
    assert r.status_code == 200 and "Retry-After" not in r.headers


# ── the claim door: the real claim_key via the AST harness ──────────────────

from tests.test_claim_key_no_ip_handback import _Cur as _ClaimCur, _run as _claim_run  # noqa: E402


class _LimitedClaimCur(_ClaimCur):
    def __init__(self, counts, **kw):
        super().__init__(**kw)
        self.counts = counts

    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        if "count(*) filter" in q and "metadata->>'ip' = %(ip)s" in q:
            self.queries.append((q, params))
            self._last = self.counts
            return
        return super().execute(sql, params)


def test_claim_over_its_ceiling_is_a_429_and_mints_nothing():
    cur = _LimitedClaimCur(_row(ip_h=10, ip_d=10))
    j, status, cur = _claim_run(body={"client_name": "pentest13"}, cur=cur)
    assert status == 429, j
    assert j["error"] == "mint_rate_limited" and j["retry_after"] > 0
    assert not cur.inserted


def test_claim_under_its_ceiling_still_mints():
    cur = _LimitedClaimCur(_row(ip_h=1, ip_d=1))
    j, status, cur = _claim_run(body={"client_name": "agent"}, cur=cur)
    assert status == 200 and j["api_key"].startswith("dch_live_")
    assert cur.inserted


# ── log-only first (owner decision 2026-09-24) ──────────────────────────────
# Unless DCHUB_MINT_RL_MODE is exactly "enforce", a caller over a ceiling is
# logged and minted as usual. The ceilings' arithmetic is unchanged.

@pytest.mark.parametrize("mode", ["", "log", "1", "true", "ENFORCED", "off"])
def test_not_enforce_means_log_only(monkeypatch, mode):
    monkeypatch.setenv("DCHUB_MINT_RL_MODE", mode)
    assert mint_guard.mint_rate_enforced() is False


def test_unset_mode_is_log_only(monkeypatch):
    monkeypatch.delenv("DCHUB_MINT_RL_MODE", raising=False)
    assert mint_guard.mint_rate_enforced() is False


@pytest.mark.parametrize("mode", ["enforce", " Enforce "])
def test_enforce_enforces(monkeypatch, mode):
    monkeypatch.setenv("DCHUB_MINT_RL_MODE", mode)
    assert mint_guard.mint_rate_enforced() is True


def test_log_only_trial_door_mints_and_logs_the_refusal_it_would_make(caplog):
    with caplog.at_level("WARNING", logger="mint_guard"):
        out, cur = _mint(_row(ip_h=10, ip_d=10), env={"DCHUB_MINT_RL_MODE": ""})
    assert out.get("ok") is True and out.get("api_key"), out
    assert cur.inserted, "log-only must mint as usual"
    lines = [r.getMessage() for r in caplog.records if "mint_rate_would_refuse" in r.getMessage()]
    assert len(lines) == 1, caplog.text
    assert "source=trial" in lines[0] and "scope=ip" in lines[0] and "mode=log" in lines[0]
    assert "203.0.113.9" not in lines[0] and "Grok" not in lines[0], "no raw IP/UA in the log line"


def test_log_only_under_the_ceiling_logs_nothing(caplog):
    with caplog.at_level("WARNING", logger="mint_guard"):
        out, _ = _mint(_row(ip_h=1, ip_d=1), env={"DCHUB_MINT_RL_MODE": ""})
    assert out.get("ok") is True
    assert "mint_rate_would_refuse" not in caplog.text


def test_log_only_claim_door_mints(monkeypatch):
    monkeypatch.setenv("DCHUB_MINT_RL_MODE", "")
    cur = _LimitedClaimCur(_row(ip_h=10, ip_d=10))
    j, status, cur = _claim_run(body={"client_name": "agent"}, cur=cur)
    assert status == 200 and j["api_key"].startswith("dch_live_"), j
    assert cur.inserted


def test_both_doors_go_through_the_mode_switch():
    """A door calling check_mint_rate directly would ignore the mode."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in ("routes/auto_trial.py", "flask_mcp_endpoints.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "mint_rate_decision(" in src, f
        assert "check_mint_rate(" not in src, f


# ── r-mint-gateway-id (2026-09-24): the gateway forwards its caller ─────────
# Measured: the UA over the 500/day ceiling was "node" (546 mints, 12 tools),
# the real UA of Node-based MCP clients and Smithery's proxy — many callers in
# one string — and request_ip_hash was the gateway's egress (17 hashes).

import hashlib as _hl

GW = {"X-Internal-Key": "k-int"}
GW_ENV = {"DCHUB_INTERNAL_KEY": "k-int"}


def _h(ip):
    return _hl.sha256(ip.encode()).hexdigest()[:16]


def _count_params(cur):
    q = [p for q, p in cur.queries if "count(*) filter" in q]
    return q[0] if q else None


def test_gateway_forwarded_ip_is_the_callers_ip_for_the_limit_and_the_row():
    out, cur = _mint(_row(), headers={**GW, "X-DCHub-Client-IP": "198.51.100.7",
                                       "X-MCP-Platform": "claude"}, env=GW_ENV)
    assert out["ok"] is True, out
    assert _count_params(cur)["ip"] == _h("198.51.100.7")
    assert cur.inserted and cur.inserted[0][2] == _h("198.51.100.7"), cur.inserted


def test_the_forwarded_ip_is_ignored_without_the_internal_key():
    out, cur = _mint(_row(), headers={"X-DCHub-Client-IP": "198.51.100.7"})
    assert out["ok"] is True
    assert cur.inserted[0][2] == _h("203.0.113.9")          # CF-Connecting-IP, not the header
    out, cur = _mint(_row(), headers={"X-Internal-Key": "guess", "X-DCHub-Client-IP": "198.51.100.7"},
                     env=GW_ENV)
    assert cur.inserted[0][2] == _h("203.0.113.9")


@pytest.mark.parametrize("bad", ["not-an-ip", "1.2.3", "x" * 80, "198.51.100.7, 10.0.0.1"])
def test_a_forwarded_value_that_is_not_an_ip_is_ignored(bad):
    out, cur = _mint(_row(), headers={**GW, "X-DCHub-Client-IP": bad}, env=GW_ENV)
    assert out["ok"] is True
    assert cur.inserted[0][2] == _h("203.0.113.9")
    assert _count_params(cur)["ip"] is None                 # no forwarded IP: gateway IP never counted


def test_a_gateway_caller_over_its_ip_ceiling_is_refused_when_enforcing():
    out, cur = _mint(_row(ip_h=10, ip_d=10),
                     headers={**GW, "X-DCHub-Client-IP": "198.51.100.7", "X-MCP-Platform": "cursor"},
                     env=GW_ENV)
    assert out["reason"] == "rate_limited" and out["scope"] == "ip", out
    assert not cur.inserted


@pytest.mark.parametrize("platform", ["smithery", "grok", "connectors-manager", "SMITHERY"])
def test_a_shared_egress_platform_is_never_ip_limited(platform):
    out, cur = _mint(_row(ip_h=10**6, ip_d=10**6),
                     headers={**GW, "X-DCHub-Client-IP": "198.51.100.7", "X-MCP-Platform": platform},
                     env=GW_ENV)
    assert out["ok"] is True, out
    assert _count_params(cur)["ip"] is None


@pytest.mark.parametrize("ua", ["node", "", "undici", "node-fetch/1.0", "Node"])
def test_a_pooled_runtime_ua_is_never_ua_limited_on_the_gateway(ua):
    out, cur = _mint(_row(ua_h=10**6, ua_d=10**6),
                     headers={**GW, "User-Agent": ua, "X-DCHub-Client-IP": "198.51.100.7",
                              "X-MCP-Platform": "claude"}, env=GW_ENV)
    assert out["ok"] is True, out
    assert _count_params(cur)["ua"] is None


def test_a_named_client_ua_is_still_ua_limited_on_the_gateway():
    out, _ = _mint(_row(ua_h=10**6, ua_d=10**6),
                   headers={**GW, "User-Agent": "Claude-User/1.0", "X-MCP-Platform": "claude"},
                   env=GW_ENV)
    assert out["reason"] == "rate_limited" and out["scope"] == "ua", out


def test_a_direct_caller_is_limited_exactly_as_before():
    """No internal key: IP and UA both count, the header changes nothing."""
    from routes.mint_guard import trial_mint_scopes

    class R:
        headers = {"User-Agent": "node"}
    assert trial_mint_scopes(R(), "node", "") == (True, True)


# ── the platform stamp (2026-09-25): our MCP harness is labelled at mint ─────

def _insert_sql(cur):
    return [q for q, _ in cur.queries if q.startswith("insert into auto_trial_keys")]


def test_the_gateway_stamps_the_agents_mcp_platform():
    out, cur = _mint(_row(), headers={"X-Internal-Key": "k-int",
                                      "X-MCP-Platform": "DCHub-Internal"},
                     env={"DCHUB_INTERNAL_KEY": "k-int"})
    assert out["ok"] is True, out
    assert "mcp_platform" in _insert_sql(cur)[0]
    assert cur.inserted[0][-1] == "dchub-internal"


def test_a_direct_caller_cannot_label_its_own_mint():
    out, cur = _mint(_row(), headers={"X-MCP-Platform": "dchub-internal"})
    assert out["ok"] is True, out
    assert cur.inserted[0][-1] is None, "only the gateway's header is trusted"


class _NoPlatformColumnCur(_TrialCur):
    """auto_trial_keys before _ensure_schema managed to add mcp_platform."""
    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        if q.startswith("insert into auto_trial_keys") and "mcp_platform" in q:
            self.queries.append((q, params))
            raise Exception('column "mcp_platform" of relation "auto_trial_keys" does not exist')
        return super().execute(sql, params)


def test_a_missing_platform_column_never_fails_the_mint():
    import routes.auto_trial as at
    cur = _NoPlatformColumnCur(_row())
    h = {"CF-Connecting-IP": "203.0.113.9", "User-Agent": "Grok/1.0"}
    with mock.patch.object(at, "_conn", lambda: _TrialConn(cur)), \
         mock.patch.object(at, "_ensure_schema", lambda c: None):
        out = at.mint_trial_for_request(_Req(h), "get_market_intel")
    assert out["ok"] is True and out["api_key"].startswith("dch_trial_"), out
    ins = _insert_sql(cur)
    assert len(ins) == 2 and "mcp_platform" in ins[0] and "mcp_platform" not in ins[1]
    assert len(cur.inserted) == 1
