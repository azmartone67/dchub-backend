"""
DC Hub — Flask MCP key validation + telemetry + dev-signup + dashboard endpoints
─────────────────────────────────────────────────────────────────────────────
Drop into the Railway Flask backend. In main.py:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)

Endpoints (all under mcp_bp):
    POST /api/v1/keys/validate    (internal)  validate dev key, return tier
    POST /api/v1/mcp/track        (internal)  log a tool-call telemetry row
    GET  /api/v1/mcp/stats        (internal)  rolled-up stats (last N days)
    POST /api/v1/dev-signup       (public)    self-serve free dev key by email
    GET  /api/v1/mcp/funnel       (public)    aggregate KPIs for dashboard
    GET  /api/v1/mcp/dashboard    (public)    serves static/mcp-dashboard.html

Required env:
    NEON_DATABASE_URL    Postgres connection string
    DCHUB_INTERNAL_KEY   shared secret for internal endpoints

Dependencies:
    psycopg[binary]>=3.2       (no _pool extra needed)
"""

import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

# 2026-06-07 fix: this import used to live INSIDE the docstring above
# (between two `    `-indented lines) so it was never actually executed.
# Every call to _require_internal raised NameError → 500 with body
# `{"error": "name 'accepted_internal_keys' is not defined"}`. That's why
# every PAID_ONLY MCP tool call resolved current_tier="free" — server.mjs
# called /api/v1/keys/validate, got a 500, cached the failure as "free".
from internal_auth import accepted_internal_keys

# SINGLE SOURCE of the mcp_tool_calls de-loop. mcp_funnel()'s
# `tool_calls_7d_real` and routes/funnel_health both import the SAME
# PLATFORM_CASE classifier + PROBE_PLATFORMS from here, so the two honest
# counts are byte-identical. See mcp_calls_deloop.py + the byte-identity test
# in tests/test_funnel_health_deloop.py.
# SINGLE SOURCE of the human_acted stage definition. The version and the
# changelog are published from routes/handoff_definition, not typed here — see
# that module for the four surfaces that used to restate it and rotted.
from routes.handoff_definition import (
    HUMAN_ACTED_DEFINITION_VERSION as _HUMAN_ACTED_VERSION,
    biggest_leak as _biggest_leak,
    biggest_leak_detail as _biggest_leak_detail,
    human_acted_definition as _human_acted_definition,
    redeem_stage_basis as _redeem_stage_basis,
)
from routes.evidence_status import (  # noqa: E402
    HYPOTHESIS as _EV_HYPOTHESIS,
    OBSERVED as _EV_OBSERVED,
    VERIFIED as _EV_VERIFIED,
    stamp as _ev_stamp,
    vocabulary_block as _ev_vocabulary,
)
from routes.handoff_definition import (
    high_intent_basis as _high_intent_basis,
    live_high_intent_threshold as _live_high_intent_threshold,
)
from mcp_calls_deloop import (
    PLATFORM_CASE as _DELOOP_PLATFORM_CASE,
    PROBE_PLATFORMS as _DELOOP_PROBE_PLATFORMS,
    real_calls_predicate as _deloop_real_calls_predicate,
    real_ua_predicate as _deloop_real_ua_predicate,
    external_platform_predicate as _deloop_external_platform_predicate,
    external_session_predicate as _deloop_external_session_predicate,
    self_traffic_session_prefixes as _deloop_self_traffic_prefixes,
    SELF_TRAFFIC_SESSION_SEED_V4 as _DELOOP_SELF_SEED_V4,
    normalize_write_platform as _normalize_write_platform,
    canonical_external_activity_sql as _canonical_activity_sql,
    CANONICAL_AGENTS_BASIS as _CANONICAL_AGENTS_BASIS,
    canonical_top_caller_sql as _canonical_top_caller_sql,
    CANONICAL_TOP_CALLER_BASIS as _CANONICAL_TOP_CALLER_BASIS,
    CANONICAL_NET_OF_TOP_CALLER_BASIS as _CANONICAL_NET_BASIS,
    CONCENTRATION_PCT as _CONCENTRATION_PCT,
    split_conversion_attribution as _split_conversion_attribution,
    CANONICAL_SIGNALS_BASIS as _CANONICAL_SIGNALS_BASIS,
    canonical_harvester_split_sql as _canonical_harvester_split_sql,
    CANONICAL_NET_OF_HARVESTER_BASIS as _CANONICAL_HARVESTER_BASIS,
    HARVESTER_PLATFORMS as _HARVESTER_PLATFORMS,
)

# Compat: prefer psycopg (v3), fall back to psycopg2 if Railway only has the older one
try:
    import psycopg
    _PSYCOPG_VERSION = 3
except ImportError:
    import psycopg2 as psycopg  # type: ignore
    _PSYCOPG_VERSION = 2
from flask import Blueprint, Response, jsonify, request

mcp_bp = Blueprint("mcp_bp", __name__)

# Module-level UUID matcher (session_ids sometimes leak into the platform
# column upstream; we drop those so platform counts stay honest). Defined at
# module scope so any handler can use it. Local copies elsewhere predate this.
import re as _re_mod
from routes._swallowed_writes import note_swallowed_write
_UUID_RE_MOD = _re_mod.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ── Self-heal / synthetic-client DIET (Item 5, 2026-06-13) ──────────────────
# platform='dchub-selfheal' was ~33k of ~36k mcp_tool_calls/wk (~93% of the
# table). Those are our OWN MCP self-probe loop (server.mjs forwards
# clientInfo.name='dchub-selfheal'; UA='node'; resolved via mcp_sessions). Every
# downstream analytics reader already EXCLUDES these names at read time (see the
# _INTERNAL_PLATFORMS / NOT LIKE 'dchub-%%' clauses below and in
# routes/mcp_funnel_diag.py, visitor_intelligence.py, mcp_analytics_postgres.py).
# So the rows are pure write-amplification: they bloat the table, slow every
# aggregate scan, and force each reader to carry the same heavy filter. Fix:
# tag-and-exclude at WRITE time — skip the legacy mcp_tool_calls insert for these
# synthetic clients. The tool still executes and returns normally (the health
# check is unaffected — it never reads this analytics row); only the polluting
# audit row is dropped. The canonical telemetry tables (mcp_call_log /
# mcp_connections via log_mcp_connection) are untouched, so internal probe
# health is still observable there if ever needed.
_SELFHEAL_SYNTHETIC_NAMES = frozenset({
    'dchub-selfheal', 'dchub-mcp-test', 'dchub-regression-test',
    'mcp-probe', 'mcp-test', 'pipeline_mcp', 'canary',
    'mcp-remote-fallback-test', 'registry-health-checker',
    'mcp-shield-scanner', 'yellowmcp-health', 'glama-health',
    'chiark-prober', 'fabrique-noauth-probe', 'agentpulse',
    'mcpscoringengine', 'mcp-extractor',
})
# Prefix families mirroring the canonical read-time filter (dchub-* covers
# selfheal/scheduler/schema-audit/failoverprobe; loop*/local-agent-mode are
# brain loops; *-probe/-health/-scanner/-checker are registry crawlers).
_SELFHEAL_SYNTHETIC_PREFIXES = ('dchub-', 'loop', 'local-agent-mode',
                                'leakaudit', 'trial-leak')
_SELFHEAL_SYNTHETIC_SUFFIXES = ('-probe', '-health', '-scanner', '-checker')


def _is_selfheal_synthetic(*names) -> bool:
    """True if ANY supplied identity (platform / client_name) is one of our own
    self-heal / synthetic probe clients. Used to skip the legacy mcp_tool_calls
    write so the analytics table reflects real external agent demand, not our
    own monitoring loop. Mirrors the read-time exclusion list so the write-time
    and read-time views stay consistent."""
    for n in names:
        if not n:
            continue
        v = str(n).strip().lower()
        if not v:
            continue
        if v in _SELFHEAL_SYNTHETIC_NAMES:
            return True
        if v.startswith(_SELFHEAL_SYNTHETIC_PREFIXES):
            return True
        if v.endswith(_SELFHEAL_SYNTHETIC_SUFFIXES):
            return True
    return False

NEON_URL     = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
INTERNAL_KEY = os.environ.get("DCHUB_INTERNAL_KEY", "")

if not NEON_URL:
    raise RuntimeError("NEON_DATABASE_URL (or DATABASE_URL) must be set for flask_mcp_endpoints")


# ── Connection helper (no pool — plain psycopg.connect per request) ────────

@contextmanager
def _conn_ctx():
    if _PSYCOPG_VERSION == 3:
        conn = psycopg.connect(NEON_URL, autocommit=True)
    else:
        conn = psycopg.connect(NEON_URL)
        conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


class _PoolShim:
    """Backward-compatible shim so existing `_pool.connection()` calls work."""
    def connection(self):
        return _conn_ctx()


def _open_track_conn():
    """Open ONE fresh autocommit connection for track_tool_call to reuse across
    its attribution read + mcp_call_log write (was two fresh connects/call).
    Autocommit → each statement is independent, so a failed read never leaves
    the connection in an aborted-transaction state for the subsequent write."""
    if _PSYCOPG_VERSION == 3:
        return psycopg.connect(NEON_URL, autocommit=True)
    conn = psycopg.connect(NEON_URL)
    conn.autocommit = True
    return conn


_pool = _PoolShim()


# r-claim-session-bind (2026-07-21): per-session claimed-key resolver for the
# /track ACTIVATION fix (track_tool_call below). claim_free_key stamps the MCP
# session_id onto the key it mints; this resolves "which key does THIS session
# own" so a post-claim call that carries no api_key still attributes to the key.
# Positive cache 5min, negative 30s (a key claimed mid-session is picked up fast
# without hammering the busiest endpoint). Recency-bounded (24h) so the lookup
# stays a cheap index hit.
_SESSION_CLAIMED_KEY_CACHE: dict = {}   # sid -> (api_key_or_None, ts)


def _resolve_session_claimed_key(conn, sid):
    if not sid or conn is None:
        return None
    import time as _t
    now = _t.time()
    _hit = _SESSION_CLAIMED_KEY_CACHE.get(sid)
    if _hit is not None:
        _val, _ts = _hit
        if now - _ts < (300 if _val else 30):
            return _val
    _val = None
    try:
        with conn.cursor() as _kc:
            _kc.execute(
                "SELECT api_key FROM mcp_dev_keys "
                "WHERE metadata->>'session_id' = %s AND status='active' "
                "AND created_at > NOW() - INTERVAL '24 hours' "
                "ORDER BY created_at DESC LIMIT 1",
                (str(sid)[:200],),
            )
            _row = _kc.fetchone()
            if _row and _row[0]:
                _val = str(_row[0])
    except Exception:
        _val = None
    if len(_SESSION_CLAIMED_KEY_CACHE) > 20000:
        _SESSION_CLAIMED_KEY_CACHE.clear()
    _SESSION_CLAIMED_KEY_CACHE[sid] = (_val, now)
    return _val


# ── r-session-restore (2026-07-26, claim-carry wave) ───────────────────────
# The mcp-server's session→key binding lives in in-memory sessionMeta PER
# REPLICA, so an agent that claimed a key mid-session silently loses its
# identity on replica rotation or a server restart — the confirmed remaining
# half of the claim-carry leak (50% of post-claim sessions carried the key;
# the /track resolver above patches TELEMETRY only, not live tier). This
# internal endpoint lets the mcp-server ask "does this session own a
# recently-claimed key?" and re-adopt it live. Gated by the same
# X-Internal-Key the worker↔backend sync uses; serves from
# _resolve_session_claimed_key's cache, so the hot path costs one dict hit.

@mcp_bp.get("/api/v1/mcp/session-key")
def mcp_session_key():
    _sent = request.headers.get("X-Internal-Key", "") or ""
    if not INTERNAL_KEY or _sent != INTERNAL_KEY:
        return jsonify(ok=False, error="internal key required"), 401
    sid = (request.args.get("session_id") or "").strip()[:200]
    if not sid:
        resp = jsonify(ok=True, api_key=None)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    conn = None
    try:
        conn = _open_track_conn()
        key = _resolve_session_claimed_key(conn, sid)
    except Exception:
        key = None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    resp = jsonify(ok=True, api_key=key or None)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── r-streak (2026-07-18): return-streak surfacing helper ──────────────────
# The progressive daily-cap unlock (return_streak.py) only moves the key-reuse
# needle if agents KNOW about it. Fail-open static ladder text for claim /
# identify payloads — never raises, never blocks a response.

def _streak_ladder_text():
    try:
        from return_streak import ladder_text
        return ladder_text()
    except Exception:
        return ("Progressive unlock — daily caps grow with your return streak: "
                "2+ active days in the trailing 14 = 1.5x, 4+ = 2x, 7+ = 3x. "
                "Keep this key and return tomorrow to climb.")


# ── Internal-only auth decorator ───────────────────────────────────────────

def _require_internal(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # phase9h_tolerant: accept the call if any of these match.
        # Background: the Worker (dchub-mcp-server) ships X-Internal-Key
        # from its INTERNAL_KEY env var. The Flask side reads
        # DCHUB_INTERNAL_KEY (the configured value). After
        # the 4/30 rewrite, the two env vars drifted and every telemetry
        # POST got 403. mcp_tool_calls hasn't filled since 4/28.
        # Tolerant matches:
        #   1) any value the operator considers internal (env vars + literal default)
        #   2) shape-aware bypass for the telemetry-only /track route
        _sent = request.headers.get('X-Internal-Key', '') or ''
        _allowed = accepted_internal_keys()
        for _name in ('DCHUB_INTERNAL_KEY', 'INTERNAL_KEY', 'MCP_INTERNAL_KEY'):
            _v = os.environ.get(_name)
            if _v: _allowed.add(_v)
        _ok = bool(_sent) and _sent in _allowed
        if not _ok:
            # shape-aware bypass: telemetry-only on /track
            try:
                _path = (request.path or '')
                if _path.endswith('/track'):
                    _j = request.get_json(silent=True) or {}
                    if (_j.get('tool_name') and
                        isinstance(_j.get('response_time_ms', _j.get('duration_ms', 0)), (int, float))):
                        _ok = True
            except Exception:
                pass
        if not _ok:
            return jsonify({'error': 'forbidden'}), 403
        return fn(*args, **kwargs)
    return wrapper


# ★ PERF (2026-09-05): 30-SECOND RESULT CACHE.
# This route runs _win() three times (24h / 7d / 30d) and each _win is 19
# sequential COUNT(DISTINCT ...) queries — 57 round trips per request, measured
# at 3.3s, on an endpoint the /ai dashboard polls every 30 seconds. The page was
# losing the race on a cold load and rendering its "Funnel unavailable" state.
#
# The TTL matches the page's own refresh interval, so a viewer never sees a
# figure older than one poll cycle.
#
# ★ THE AGE IS PUBLISHED, not implied. `cache` in the payload carries age_s and
#   ttl_s, so a reader can tell a 29-second-old number from a live one. A cache
#   that hides its age turns a stale read into an unfalsifiable claim, which is
#   the defect this whole endpoint exists to avoid.
# ★ FAILURES ARE NEVER CACHED. Only ok=True results are stored, so a transient
#   DB error cannot be served for 30 seconds after it has cleared — and an
#   "unavailable" is always a live verdict, never a remembered one.
# Kill switch: HANDOFF_FUNNEL_CACHE_TTL=0
_FUNNEL_CACHE = {"payload": None, "ts": 0.0}


def _funnel_ttl():
    try:
        return max(0, int(os.environ.get("HANDOFF_FUNNEL_CACHE_TTL", "30")))
    except Exception:  # noqa: BLE001
        return 30


# ── GET /api/v1/mcp/handoff-funnel ──────────────────────────────────────────
# r-handoff (2026-06-21): the agent→human conversion funnel, end to end, on
# DISTINCT sessions — surfaces WHERE the handoff leaks. paywall-hit → high-intent
# → relay artifact minted (claim_token) → human acted (claim used) → identified
# (email) → paid (attributed). Built from existing tables, read-only, fail-soft,
# aggregate-only (no PII). 2026-06-21 baseline (30d): 1582 → 675 → 32 → 1 → 1 → 0:
# the handoff dies at relay-mint (only ~5% of high-intent sessions ever mint a
# relay artifact) and paid attribution is empty.
@mcp_bp.get("/api/v1/mcp/handoff-funnel")
def handoff_funnel():
    def _win(cur, days):
        iv = "%d days" % int(days)
        def one(sql):
            try:
                cur.execute(sql); r = cur.fetchone()
                return int(r[0]) if r and r[0] is not None else 0
            except Exception:
                return None
        paywall = one("select count(distinct session_id) from mcp_upgrade_signals "
                      "where created_at > now() - interval '%s' "
                      "and signal_type in ('trial_preview','paid_tool_blocked') "
                      "and session_id is not null" % iv)
        high    = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                      "where first_hit_at > now() - interval '%s'" % iv)
        minted  = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                      "where claim_minted_at is not null and first_hit_at > now() - interval '%s'" % iv)
        used    = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                      "where claim_used_at is not null and first_hit_at > now() - interval '%s'" % iv)
        # r-funnel-honest (2026-06-25): 'human_acted' previously read claim_used_at,
        # but that is dominated by the SERVER-SIDE auto-redeem (server.mjs
        # _autoRedeemClaim stamps it ~1s after mint — no human, no browser; the real
        # human-click instrument claim_page_opened_at has fired 0× all-time). Read
        # the GET-/claim instrument for human_acted; keep claim_used_at as a separate
        # 'redeemed' diagnostic (human form-submit OR machine auto-redeem).
        #
        # Shell #44 r-two-artifacts (2026-07-30) — human_acted DEFINITION v2:
        # v1's instrument was structurally unmeasurable — the /claim link was
        # single-use and the gateway burned it in median 0.85s, so a human
        # click could only ever land on a 410. The relay now mints a SECOND,
        # human-audience artifact (/relay/<token>: 7d TTL, multi-open, binds
        # nothing) and human_acted reads ITS open-stamp. v1's instrument is
        # kept below as a labelled legacy diagnostic. Weeks that span
        # 2026-07-30 mix an unmeasurable stage with a measurable one — that
        # is exactly what the definition block declares.
        #
        # r-both-artifacts (2026-08-16) — human_acted DEFINITION v3: v2 was
        # blind in one eye and credulous in the other. Blind: the link agents
        # actually show humans is /upgrade/h/<payload>.<sig> (server.mjs
        # buildHumanRelay → for_your_human), whose opens log to relay_opens
        # (routes/human_relay.py) — v2 never read that table, so a real human
        # click on the relayed link could not move this stage. Credulous: v2
        # counted every /relay/<token> stamp, and all 4 all-time "opens" were
        # probes (cursor render-verify, Grok probes, an indexer); relay_opens'
        # 2 all-time rows were our own probes too. v3 is the union of BOTH
        # artifacts' first-opens on real UAs only (mcp_calls_deloop.
        # real_ua_predicate — the same canonical families every other real-
        # traffic read uses). The /relay side gets a new UA instrument
        # (human_view_first_ua, stamped by relay_view on the first real-UA
        # open); pre-v3 stamps carry no UA and are excluded — correct here,
        # since all of them are verified probes. relay_opens rows join on
        # session_id = mcp_session_id (the decoded token sid; invalid-token
        # opens store NULL and self-exclude — and blank sids must not join
        # either: the token contract mints with sid='' happily, so relay_opens
        # carries a valid blank-sid probe row that would flip any blank-sid
        # session to human_acted). Both queries stay fail-soft and
        # the connection is autocommit, so a missing relay_opens table nulls
        # this stage without poisoning later reads.
        #
        # r-selftraffic-funnel (2026-08-17) — human_acted DEFINITION v4: v3
        # excluded PROBES (by UA) but not the OPERATOR. On 2026-08-17 the
        # metric went 0 → 1 for the first time in its life, and the 1 was a
        # deliberate verification open by the operator's own browser on the
        # operator's own session (88e20dac, mcp_client 'claude', UA 'node' —
        # see mcp_calls_deloop.external_session_predicate for why this cannot
        # be inferred from the row). A first non-zero on a stage that has never
        # fired is the single most misreadable number on this dashboard: it
        # reads as "the handoff converted". v4 subtracts known self-traffic so
        # the first non-zero, when it comes, is somebody else.
        #
        # The v3 figure is kept alongside as human_acted_v3_including_self_traffic
        # and the exclusion is published in `excluded` — this stage is never
        # reduced silently.
        _hv_real = _deloop_real_ua_predicate("s.human_view_first_ua")
        _ro_real = _deloop_real_ua_predicate("ro.user_agent")
        _not_self = _deloop_external_session_predicate("s.mcp_session_id")
        _v3_body = (
            "from mcp_high_intent_sessions s "
            "where s.first_hit_at > now() - interval '%s' and "
            "((s.human_view_first_opened_at is not null and "
            "s.human_view_first_ua is not null and " + _hv_real + ") "
            "or exists (select 1 from relay_opens ro where "
            "ro.session_id = s.mcp_session_id and ro.session_id <> '' "
            "and " + _ro_real + "))")
        opened = one(("select count(distinct s.mcp_session_id) "
                      + _v3_body + " and " + _not_self) % iv)
        opened_v3 = one(("select count(distinct s.mcp_session_id) " + _v3_body) % iv)
        # r-seed-rotation (2026-09-03) — human_acted DEFINITION v5. v4 named
        # ONE operator session and the operator's client rotated its id on
        # 2026-08-20 (8c8e1d0d, first call 10.1s after 88e20dac's last, same
        # platform/UA/tier). That session opened a relay link and this stage
        # published 1 over 30d — the first non-zero in its life, and v4 exists
        # to stop that being us. v4's own figure is kept alongside, DERIVED
        # from the named v4-era seed so no session id is retyped here.
        _not_self_v4 = _deloop_external_session_predicate(
            "s.mcp_session_id", _DELOOP_SELF_SEED_V4)
        opened_v4 = one(("select count(distinct s.mcp_session_id) "
                         + _v3_body + " and " + _not_self_v4) % iv)
        opened_v2 = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                        "where human_view_first_opened_at is not null and first_hit_at > now() - interval '%s'" % iv)
        opened_legacy = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                            "where claim_page_opened_at is not null and first_hit_at > now() - interval '%s'" % iv)
        emailed = one("select count(distinct mcp_session_id) from mcp_high_intent_sessions "
                      "where claim_email is not null and claim_email <> '' "
                      "and first_hit_at > now() - interval '%s'" % iv)
        # r-funnel-honest2 (2026-06-26): the per-session 'identified' (emailed) only
        # counts emails bound INSIDE a high-intent session — but most identity capture
        # happens on the key tables (claim_free_key, direct bind_email) with NO session
        # link, so 'identified' structurally undercounts. Surface the REAL distinct-email
        # capture as a sibling top-line so the dashboard stops implying zero identity
        # (verified: ~12 distinct emails/30d the per-session funnel never saw).
        captured = one("select count(distinct lower(email)) from mcp_dev_keys "
                       "where email is not null and email <> '' "
                       "and created_at > now() - interval '%s'" % iv)
        paid_su = one("select count(distinct mcp_session_id) from mcp_session_upgrades "
                      "where upgraded_at > now() - interval '%s'" % iv)
        paid_tp = one("select count(distinct mcp_session_id) from mcp_topups "
                      "where mcp_session_id is not null and created_at > now() - interval '%s'" % iv)
        paid = (paid_su or 0) + (paid_tp or 0)
        # Bound ONCE per window, beside the block that publishes it, so the
        # basis sentence and the stage it describes cannot disagree.
        _hi_threshold = _live_high_intent_threshold()

        # ── r-relay-provenance (2026-09-03) ──────────────────────────────
        # WHY: `relay_opens` held 150 rows over 30d and the funnel published
        # human_acted off it while saying nothing about what those rows ARE.
        # Measured decomposition on 2026-09-03 (30d, 150 rows), using the
        # SAME predicates the queries below apply — not a hand count, which
        # is how a published basis drifts from the query it describes:
        #
        #     123  probe_ua        fails real_ua_predicate. Bulk is our own
        #                          dchub-qa-superuser sweep (~6/day, one
        #                          fabricated session id per open); the rest
        #                          are curl / Go-http-client / urllib.
        #      25  no_session_id   real UA, blank token sid — scanners
        #                          hitting /upgrade/h/<junk>, joinable to
        #                          nothing.
        #       2  countable       real UA AND a joinable sid. TWO. Both are
        #                          the operator's own sessions (88e20dac and
        #                          its 08-20 rotation 8c8e1d0d).
        #
        # So over thirty days exactly two rows were even ELIGIBLE for this
        # stage, and both are ours: no relay link has yet been opened by
        # anyone who received it from an agent. A reader seeing
        # "150 opens" and a 0-or-1 stage cannot tell whether the instrument
        # is broken or the demand is absent; publishing the split answers
        # that without a code dive. The three buckets are MUTUALLY EXCLUSIVE
        # and exhaust the window, so they sum to `total`.
        _ro_win = "from relay_opens ro where ro.ts > now() - interval '%s'"
        _sid_ok = "coalesce(ro.session_id,'') <> ''"
        prov_total = one(("select count(*) " + _ro_win) % iv)
        prov_probe = one(("select count(*) " + _ro_win
                          + " and not " + _ro_real) % iv)
        prov_nosid = one(("select count(*) " + _ro_win
                          + " and " + _ro_real + " and not " + _sid_ok) % iv)
        prov_real = one(("select count(*) " + _ro_win
                         + " and " + _ro_real + " and " + _sid_ok) % iv)
        prov_real_sids = one(("select count(distinct ro.session_id) " + _ro_win
                              + " and " + _ro_real + " and " + _sid_ok) % iv)
        # ── the stage's DENOMINATOR GAP ──────────────────────────────────
        # human_acted counts FROM mcp_high_intent_sessions and only then asks
        # whether a relay row exists. Relay links are minted on paths that
        # never write that table (the auto-trial envelope among them), so an
        # open on such a session is not a zero — it is UNCOUNTABLE, and the
        # two are indistinguishable downstream. Measured 30d on 2026-09-03:
        # 148 of 163 joinable opens sat on sessions absent from the table.
        # Published so the stage's blind spot is declared, never implied.
        gap_sids = one(("select count(distinct ro.session_id) " + _ro_win
                        + " and " + _ro_real + " and " + _sid_ok
                        + " and not exists (select 1 from"
                        " mcp_high_intent_sessions h where"
                        " h.mcp_session_id = ro.session_id)") % iv)
        def pct(n, d):
            return round(100.0 * n / d, 2) if (n is not None and d) else None
        steps = {"paywall_hit": paywall, "high_intent": high, "relay_minted": minted,
                 "human_acted": opened, "redeemed": used,
                 "identified": emailed, "paid_attributed": paid}
        return {
            "steps": steps,
            "emails_captured_total": captured,
            "human_acted_legacy_claim_page": opened_legacy,
            "human_acted_v2_all_view_opens": opened_v2,
            "human_acted_v3_including_self_traffic": opened_v3,
            "human_acted_v4_before_rotation": opened_v4,
            # ★ What the source table actually contains. Buckets are mutually
            # exclusive and sum to `total`; see the r-relay-provenance block.
            "relay_open_provenance": {
                "total": prov_total,
                "probe_ua": prov_probe,
                "no_session_id": prov_nosid,
                "countable_opens": prov_real,
                "countable_sessions": prov_real_sids,
                "basis": (
                    "relay_opens rows in the window, split by whether the row "
                    "can reach human_acted at all. probe_ua = fails "
                    "mcp_calls_deloop.real_ua_predicate (our own qa-superuser "
                    "sweep is the bulk of it, one fabricated session id per "
                    "open). no_session_id = a blank/NULL token sid, which "
                    "cannot join to any session (scanners on /upgrade/h/). "
                    "countable_opens = real UA and a joinable sid — the ONLY "
                    "rows this stage can ever see. Measured 2026-09-03 over "
                    "30d: 150 total = 123 probe_ua + 25 no_session_id + 2 "
                    "countable, and BOTH countable rows are the operator's "
                    "own sessions. Read a 0 on human_acted against "
                    "countable_opens, not against total: the stage has had "
                    "two eligible rows in a month, so its zero is a "
                    "statement about delivery, not about human appetite."),
            },
            # ★ The stage cannot see most of what it is supposed to measure.
            "human_acted_denominator_gap": {
                "countable_sessions": prov_real_sids,
                "sessions_not_in_high_intent_table": gap_sids,
                "basis": (
                    "human_acted counts FROM mcp_high_intent_sessions and only "
                    "then asks whether a relay_opens row exists, so an open on "
                    "a session that never entered that table is UNCOUNTABLE "
                    "rather than zero — and the two are indistinguishable in "
                    "the published number. Relay links ARE minted on paths "
                    "that never write the table (the auto-trial envelope among "
                    "them). Measured 2026-09-03 over 30d: 1 of the 2 "
                    "countable sessions sat outside the table; across ALL "
                    "relay_opens rows regardless of UA it is 148 of 163. "
                    "This field is the size of the blind spot, not a leak."),
            },
            # ★ The exclusion is DECLARED, never silent. A stage that has fired
            # once in its life cannot afford a reader who does not know the one
            # was us.
            "excluded": {
                "self_traffic_sessions": _deloop_self_traffic_prefixes(),
                "human_acted_removed": (
                    (opened_v3 - opened)
                    if (opened_v3 is not None and opened is not None) else None),
                # The version is INTERPOLATED, not typed: this sentence said
                # "(v4)" beside a dict that had just become v4, and the next
                # bump would have left it describing the wrong stage in the
                # same envelope that publishes the right one.
                "basis": "human_acted (v%d) subtracts sessions declared as operator "
                         "self-traffic in mcp_calls_deloop. The operator's own agent "
                         "client writes an mcp_client/user_agent byte-identical to a "
                         "prospect's, so nothing in the row itself gives it away and "
                         "every removal is published here to be audited or added back. "
                         "★ MIXED BASIS since v5: 88e20dac is a NAMED FACT (we watched "
                         "ourselves make it); 8c8e1d0d is an INFERENCE from a 10.1-second "
                         "session rotation off that same client on 2026-08-20, which the "
                         "operator could neither confirm nor deny. It is excluded because "
                         "it was publishing this stage's first-ever non-zero and a false "
                         "conversion here gets quoted back by every partner who reads the "
                         "dashboard — but it is an inference and is labelled as one. "
                         "human_acted_v4_before_rotation is the figure without it; "
                         "human_acted_v3_including_self_traffic is unfiltered."
                         % _HUMAN_ACTED_VERSION,
            },
            # ONE WRITER (r-definition-one-writer, 2026-08-18). This block was
            # a dict literal here, and three other surfaces RESTATED it in
            # prose: the public /ai handoff card, adoption_master_shell and
            # handoff_truth_master_shell. The 08-17 bump to v4 moved this dict
            # and none of them, so the dashboard described v3 — a superseded
            # definition, missing the operator exclusion entirely — the day
            # after v4 shipped. The canon now lives in routes/handoff_definition
            # and every surface DERIVES from it. Nothing here restates a
            # version; see that module for the guard.
            # ★2026-08-25: paywall_hit and high_intent shipped with NO published
            # definition while human_acted and redeemed each carried a full one.
            # A reader given `paywall 24 -> high_intent 2 (8.33%)` against a 7d
            # rate of 55.22% has to reverse-engineer both stages from source to
            # know whether that is a broken instrument or a real change. It was
            # a real change, and the reason is IN the definition. ★2026-09-03:
            # that reason was stated as "high_intent requires REPEAT paid-tool
            # use, so single-call traffic cannot convert by construction" — true
            # only at threshold >= 2, and prod runs on 1. The basis now READS
            # the live threshold instead of asserting it, and declares the
            # paywall_hit filter boundary beside it. Publishing the basis is
            # what makes the number readable without a code dive; publishing a
            # basis that restates config is what makes it readable and WRONG.
            "definitions": {"human_acted": _human_acted_definition(),
                            "redeemed": _redeem_stage_basis(),
                            "paywall_hit": {
                                "basis": (
                                    "COUNT(DISTINCT session_id) FROM mcp_upgrade_signals "
                                    "WHERE signal_type IN ('trial_preview','paid_tool_blocked') "
                                    "AND session_id IS NOT NULL, over the window. One row per "
                                    "gated-tool encounter; a session that hits the wall five "
                                    "times counts ONCE."),
                                "is_funnel_progress": True,
                            },
                            "high_intent": {
                                "basis": _high_intent_basis(_hi_threshold),
                                # ★ The drop from paywall_hit is NOT all conversion loss.
                                # The two stages are drawn from differently-filtered
                                # populations, and differencing them measures the filter
                                # as if it were a leak — the misread this block exists
                                # to prevent.
                                "population_vs_paywall_hit": (
                                    "paywall_hit is written by signalPaywall() with NO bot "
                                    "filter. Entry HERE is refused at write time for "
                                    "internal/CI/probe clients and raw scripting UAs "
                                    "(routes.mcp_high_intent_claim._is_non_human_client), "
                                    "which returns HTTP 200 with skipped=<reason> and "
                                    "records nothing. Part of this drop is therefore a "
                                    "filter boundary, not a lost prospect."),
                                "is_funnel_progress": True,
                                "subset_of": "paywall_hit",
                                "measured_2026_08_25": (
                                    "Every high_intent session was also a paywall_hit session "
                                    "in both windows checked (24h 2 of 2, 7d 148 of 148; "
                                    "high_intent with no paywall row = 0), so the ratio IS a "
                                    "conversion rate and not two independent populations. "
                                    "What moves it is the share of paywall sessions that call "
                                    "more than once: 101 of 268 (37.7%) over 7d against 1 of "
                                    "24 (4.2%) in the 24h window that read 8.33%. A drop here "
                                    "is a traffic-composition signal before it is a defect."),
                            }},
            # r-evidence-status (2026-08-21). Seven AI partners reviewed this
            # payload on 08-17 and could not tell our measurements from our
            # interpretations, because both are published in the same shape.
            # Four wrong root-causes went out and all seven adopted each one
            # verbatim. The observation (human_acted == 0) never changed across
            # all four; only the unmarked story did.
            #
            # ★ Stamps are CONSERVATIVE on purpose. `verified` asserts an
            # experiment isolated a mechanism, and a wrong stamp is worse than
            # none because consumers propagate a machine-readable field without
            # the hedging that surrounds prose. Only ONE claim here is verified,
            # and it is the one an experiment actually settled.
            "evidence_status_claims": {
                "stage_counts": _ev_stamp(
                    ["paywall", "high_intent", "relay_minted", "redeemed",
                     "human_acted", "identified", "paid"],
                    _EV_OBSERVED,
                    "Distinct-session counts read directly from the tables named "
                    "in each stage's definition. Counts are measurements; what "
                    "they IMPLY is not.",
                ),
                "human_acted_instrument": _ev_stamp(
                    "functional",
                    _EV_VERIFIED,
                    "2026-08-17: opening a handoff link from a real browser moved "
                    "human_acted 0->1 on the first attempt and stamped "
                    "human_view_first_ua. An experiment isolated it. The long "
                    "run of zeros was honest, not an instrument fault — four "
                    "prior explanations for that zero were wrong.",
                ),
                # ★ NOT keyed "biggest_leak". tests/test_relay_closure_shell.py
                # asserts the value at that key is a call to the one-writer
                # _biggest_leak(); a second dict in the same payload reusing the
                # name made the AST guard read _ev_stamp and fail. The guard was
                # right — one name, one writer.
                "biggest_leak_is_an_interpretation": _ev_stamp(
                    "see .biggest_leak",
                    _EV_HYPOTHESIS,
                    "Names the largest arithmetic drop between adjacent stages. "
                    "WHY a stage drops is not established by that arithmetic, "
                    "and every published cause for this funnel's leak so far "
                    "has been wrong. Do not propagate as a finding.",
                ),
            },
            "rates": {
                "paywall_to_relay_pct": pct(minted, paywall),
                "relay_to_human_pct": pct(opened, minted),
                "relay_to_redeemed_pct": pct(used, minted),
                "paywall_to_paid_pct": pct(paid, paywall),
            },
            "biggest_leak": (
                # r-leak-truth (2026-07-15): relay→human_click WAS machine-mediated
                # BY DESIGN — server.mjs auto-redeems the claim ~1s after mint, so
                # claim_page_opened_at (human_click) is ~0 all-time and was NOT a
                # leak. Shell #44 (2026-07-30) made the stage measurable with the
                # separate human-view artifact, so relay→human is a REAL candidate
                # leak again — but a near-zero early read may mean agents don't
                # SURFACE the link to their humans (the mint payload's human_note
                # asks them to), not that humans decline. Judge after weeks, not
                # days, and against human_view_opens, not assumptions.
                # r-both-artifacts (2026-08-16): "by design" stopped being a
                # defense — mcp-server #193 measured ~96% of minted claims
                # machine-redeemed by the server's own _autoRedeemClaim in
                # median <1s (the paywall was a free-key dispenser) and turned
                # auto-redeem opt-in-off. With arbitrage stopped and v3 reading
                # both human artifacts, the open question at this stage is
                # whether agents SHOW for_your_human at all — watch relay_opens
                # + human_view_first_opened_at accumulate from 2026-08-16 on.
                # r-redeem-not-a-leak (2026-08-21): the ladder is now ONE
                # writer in routes.handoff_definition, and `redeemed` is no
                # longer on it. Its writer (_autoRedeemClaim, median 0.72s) was
                # switched off on 2026-08-16, so `used` is 0 for every window
                # that does not reach back past the cutoff and this expression
                # had degenerated into a CONSTANT reading "relay→redeemed"
                # forever — publishing a deliberately disabled machine step as
                # the funnel's biggest leak. The stage stays published as the
                # machine-arbitrage diagnostic it always was; see
                # definitions.redeemed. The live human stage is human_acted.
                _biggest_leak(steps)),
            # ★ The same ladder rung, WITH the stage keys and counts it was
            # chosen on, so a renderer never has to re-derive the cliff to
            # write a sentence about it. ai.html did re-derive it — over an
            # array that still carried `redeemed` — and published
            # "Relay minted → Redeemed, 100% lost" while `biggest_leak` beside
            # it said "relay_mint→human_acted". See biggest_leak_detail().
            "biggest_leak_detail": _biggest_leak_detail(steps),
        }
    out = {"ok": True, "metric": "agent_to_human_handoff_funnel", "unit": "distinct_sessions"}
    # Published ONCE at the top; the per-window `evidence_status_claims` blocks
    # below reference this vocabulary. A consumer reads the convention off the
    # wire instead of being told about it in a memo it will not have.
    out["evidence_status"] = _ev_vocabulary()
    _ttl = _funnel_ttl()
    _now = time.time()
    _hit = _FUNNEL_CACHE.get("payload")
    if _ttl and _hit is not None and (_now - _FUNNEL_CACHE.get("ts", 0.0)) < _ttl:
        _cached = dict(_hit)
        _cached["cache"] = {
            "hit": True,
            "age_s": round(_now - _FUNNEL_CACHE["ts"], 1),
            "ttl_s": _ttl,
            "note": ("served from a process-local result cache. 57 COUNT(DISTINCT)"
                     " round trips per miss; the TTL matches the /ai page's own"
                     " 30s refresh so a viewer never sees a figure older than one"
                     " poll. Only ok=True results are cached — an error is always"
                     " a live verdict. Kill: HANDOFF_FUNNEL_CACHE_TTL=0"),
        }
        return jsonify(_cached), 200

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            out["last_24h"] = _win(cur, 1)
            out["last_7d"] = _win(cur, 7)
            out["last_30d"] = _win(cur, 30)
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:160]
    out["cache"] = {"hit": False, "age_s": 0.0, "ttl_s": _ttl}
    if _ttl and out.get("ok"):
        _FUNNEL_CACHE["payload"] = out
        _FUNNEL_CACHE["ts"] = time.time()
    return jsonify(out), 200


# ── GET/POST /api/v1/keys/standing ─────────────────────────────────────────
# Read-only cross-session standing for a durable key (auto_trial_keys). Powers
# the MCP server's RETURNING-KEY reward: a key first minted in a PRIOR ISO week
# that is being used again is a genuine cross-session RETURNER — exactly the
# cohort the retention KPI (routes/mcp_retention.py returned_next_week) measures
# and the 0.5%-reuse leak the Optimization Engines flag. No writes; fail-soft.
@mcp_bp.get("/api/v1/keys/standing")
@mcp_bp.post("/api/v1/keys/standing")
@_require_internal
def key_standing():
    api_key = (request.args.get("api_key")
               or (request.get_json(silent=True) or {}).get("api_key") or "").strip()
    out = {"found": False, "returning": False, "age_days": 0.0,
           "call_count": 0, "weeks_active": 0}
    if not api_key:
        return jsonify(out), 200
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(call_count, 0), "
                "       EXTRACT(EPOCH FROM (now() - minted_at)) / 86400.0, "
                "       (minted_at < date_trunc('week', now())) AS before_this_week "
                "  FROM auto_trial_keys WHERE api_key = %s",
                (api_key,),
            )
            row = cur.fetchone()
        if row:
            call_count, age_days, before_this_week = row
            # RETURNING = the key predates the current ISO week (spanned >=1 week
            # boundary) AND was used beyond its first call — a real return, not a
            # one-shot. The reward (MCP side) is bounded to 1 bonus full call/day.
            returning = bool(before_this_week) and int(call_count or 0) > 1
            out = {
                "found": True,
                "returning": returning,
                "age_days": round(float(age_days or 0), 1),
                "call_count": int(call_count or 0),
                "weeks_active": int(float(age_days or 0) // 7) + 1,
            }
    except Exception as e:
        out["error"] = str(e)[:120]   # fail-soft: never block the funnel
    return jsonify(out), 200


# ── POST /api/v1/keys/validate ─────────────────────────────────────────────

# The Node gate (server.mjs applyTierGate) unlocks paid tools ONLY for
# tier in ('paid','enterprise'). So validate MUST normalize granular plan
# names (founding/pro/team/metered/…) to that vocabulary, or a paying
# customer is gated. Mirrors the Stripe webhook's _paid_mcp_tier mapping.
_ENT_PLANS  = {"enterprise", "research_seed", "admin"}
_PAID_PLANS = {"paid", "pro", "founding", "team", "metered"}

def _node_tier_max(plans):
    """Highest Node-vocab tier across a list of plan strings.
    enterprise > paid > (identified/starter/developer) > free."""
    norm = set()
    for p in plans:
        p = (p or "").strip().lower()
        if p in _ENT_PLANS:
            norm.add("enterprise")
        elif p in _PAID_PLANS:
            norm.add("paid")
        elif p:
            norm.add(p)
    if "enterprise" in norm:
        return "enterprise"
    if "paid" in norm:
        return "paid"
    for t in ("identified", "starter", "developer"):
        if t in norm:
            return t
    return "free"


# The set of values _node_tier_max can ever return. monthly_quota's tier
# map must stay a superset of this (tests/test_monthly_quota.py asserts
# it) — an unmapped Node tier is how a paying customer would silently
# inherit the free monthly quota.
NODE_TIER_VOCABULARY = ("enterprise", "paid", "identified", "starter",
                        "developer", "free")


# ── Quota/price copy, read from the registry rather than hand-typed ─────
# Agent-facing hints in this file quoted invented numbers ("100 calls/day",
# "1,000/day" for Developer, whose mcp_daily is 500) — an agent that trusts
# a cap we never grant is a support ticket at best. All three are fail-soft:
# a missing tier_registry degrades to a vaguer sentence, never a wrong one.
#
# PAID tiers are quoted per MONTH (monthly_quota.py enforces the month; the
# per-day cap was never enforced on the /mcp path). free and identified are
# quoted per DAY — those gates are real and still daily.

def _tier_calls_per_day(tier):
    try:
        from tier_registry import calls_per_day
        return f"{calls_per_day(tier):,}"
    except Exception:
        return "the free tier's"


def _tier_calls_per_month(tier):
    try:
        from tier_registry import calls_per_month
        return f"{calls_per_month(tier):,}"
    except Exception:
        return "more"


def _tier_price_label(tier):
    try:
        from tier_registry import price
        v = price(tier)
        return f"${v}/mo" if v else "custom-priced"
    except Exception:
        return "available"


def _tier_cross_check(cur, api_key, user_email, want_metered=False):
    """The highest-of-3 tier sources behind validate_key's effective tier.

    Extracted 2026-08-06 (monthly-quota phase 2) so the quota gate resolves
    a caller's tier the SAME way validate_key does instead of re-deriving
    it. mcp_dev_keys.tier is the caller's job (it owns the key row); this
    returns the other two sources plus the metered flag.

    Returns (plan_tier, api_key_tier, metered_over). Caller owns the
    cursor and the fail-soft guard — every lookup here is advisory.
    """
    plan_tier = None
    api_key_tier = None
    metered_over = False

    # users.plan via email join (most paying customers)
    if user_email:
        cur.execute(
            "SELECT plan FROM users WHERE LOWER(email) = LOWER(%s) "
            "AND subscription_status IN ('active','trialing') LIMIT 1",
            (user_email,),
        )
        ur = cur.fetchone()
        if ur and ur[0]:
            plan_tier = ur[0].lower()

    # api_keys.rate_limit_tier (covers enterprise/research_seed keys
    # minted outside the Stripe flow). 2026-07-30: this SELECTed by
    # key_value/revoked_at — columns api_keys has NEVER had (live schema:
    # key_hash + is_active INTEGER) — so it threw UndefinedColumn on every
    # call and the caller's fail-soft except swallowed it: this leg (and
    # the metered check after it) never ran. Use the same dual key_hash
    # convention as the api_keys fallback earlier in this module and
    # free_tier_gate._user_from_api_key: standard keys store sha256(key);
    # partner/admin keys store the RAW key string in key_hash.
    import hashlib as _hl2
    cur.execute(
        "SELECT rate_limit_tier FROM api_keys "
        "WHERE key_hash IN (%s, %s) "
        "AND (is_active IS NULL OR is_active = 1) LIMIT 1",
        (_hl2.sha256(api_key.encode()).hexdigest(), api_key),
    )
    ar = cur.fetchone()
    if ar and ar[0]:
        api_key_tier = ar[0].lower()

    # r-metered-enforce (2026-07-12, DARK behind MONETIZE_METERED_ENFORCE):
    # has the daily monetize cron already flagged this keyed identity over the
    # grid/fiber free threshold (metered_billing_decisions)? The Node gate
    # turns a true here into a $10 402 on the next metered call. Only runs
    # while the env switch is on; fail-open (missing table / drift → no flag).
    if want_metered and os.environ.get("MONETIZE_METERED_ENFORCE", "0") == "1":
        try:
            cur.execute(
                "SELECT 1 FROM metered_billing_decisions "
                "WHERE id_kind = 'api_key' AND identity = %s "
                "AND decision = 'over_threshold_free' LIMIT 1",
                (api_key,),
            )
            metered_over = cur.fetchone() is not None
        except Exception:
            metered_over = False

    return plan_tier, api_key_tier, metered_over


def resolve_effective_node_tier(api_key):
    """validate_key's effective tier for `api_key`, without the HTTP hop.

    Returns a NODE_TIER_VOCABULARY value, or None when the key is in none
    of the three tier tables. None is load-bearing: it means "we do not
    know what this caller bought", and the monthly-quota gate fails open
    on it rather than treating an edge-minted or comp key as free.

    Fail-soft: any DB error also returns None (fail open).
    """
    key = (api_key or "").strip()
    if not key:
        return None
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
                (key,),
            )
            row = cur.fetchone()
            mcp_tier = None
            user_email = None
            if row and row[2] == "active":
                user_email = row[0]
                mcp_tier = (row[1] or "free").lower()
            plan_tier, api_key_tier, _ = _tier_cross_check(cur, key, user_email)
    except Exception as e:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "resolve_effective_node_tier failed (failing open): %s", e)
        except Exception:
            pass
        return None

    candidates = [t for t in (mcp_tier, plan_tier, api_key_tier) if t]
    if not candidates:
        return None
    return _node_tier_max(candidates)


@mcp_bp.post("/api/v1/keys/validate")
@_require_internal
def validate_key():
    body    = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"valid": False, "tier": "free"}), 200

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT developer_id, email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
            (api_key,),
        )
        row = cur.fetchone()

    if not row or row[3] != "active":
        # r55-conv (2026-05-31): auto-trial keys (dch_trial_, minted by the
        # REST gate / /keys/auto-mint / anon-grace) live in auto_trial_keys,
        # NOT mcp_dev_keys — so they validated as invalid here and the entire
        # inline-mint funnel issued non-working keys. Recognize them via the
        # existing validate_trial_key() and report tier 'free' so the Node
        # gate's KEYED_FREE_BONUS unlock applies to the 5 demand tools (a
        # higher tier would fall through applyTierGate to the PAID_ONLY block).
        # Fail-soft: any error → original valid:false.
        try:
            from routes.auto_trial import validate_trial_key
            _ok, _reason = validate_trial_key(api_key)
            if _ok:
                # r-streak (2026-07-18): surface the return-streak state on the
                # validate hop the Node MCP server relays — the progressive
                # unlock only works if agents KNOW. Fail-open: block optional.
                _streak = None
                try:
                    from return_streak import streak_snapshot
                    _streak = streak_snapshot(api_key)
                except Exception:
                    _streak = None
                return jsonify({
                    "valid":        True,
                    "tier":         "free",
                    "developer_id": None,
                    "email":        None,
                    "source":       "auto_trial",
                    "streak":       _streak,
                }), 200
            # 2026-06-08 conversion lever: distinguish the unbound daily-cap hit
            # (15/day) so the agent gets the specific "bind email to unlock 50/day"
            # nudge at the exact moment of friction — not a bare invalid. Still
            # valid:False (Node falls back to the free-tier paywall hint), but
            # carries the bind CTA for clients that surface structured hints.
            if _reason in ("daily_cap_unbound", "daily_cap"):
                # r-streak (2026-07-18): the cap-hit moment is THE moment to
                # teach the return-streak unlock — current cap, streak days,
                # and what returning tomorrow earns. Bound keys hit this at
                # base 50/day, unbound at base 15/day (both streak-boosted in
                # validate_trial_key). Fail-open to the static hint.
                _streak = None
                _streak_hint = ""
                try:
                    from return_streak import streak_snapshot
                    from routes.auto_trial import (TRIAL_DAILY_CALLS as _B_BOUND,
                                                   TRIAL_DAILY_UNBOUND as _B_UNB)
                    _base = _B_BOUND if _reason == "daily_cap" else _B_UNB
                    _streak = streak_snapshot(api_key, _base)
                    _streak_hint = " " + _streak["message"]
                except Exception:
                    _streak, _streak_hint = None, ""
                return jsonify({
                    "valid":  False,
                    "tier":   "free",
                    "reason": _reason,
                    "streak": _streak,
                    "upgrade_hint": (
                        "DC Hub trial daily cap reached. Bind your operator's "
                        "email to unlock 50 calls/day: POST "
                        "/api/v1/keys/auto-trial/bind {api_key, email}. "
                        "(Agents can't pay — the email is how the upgrade reaches "
                        "your human.)" + _streak_hint),
                }), 200
            # 2026-07-10 (funnel audit): bind_email_required — the ONE gate with
            # teeth (10 cumulative unbound calls) — previously fell through to a
            # bare {valid:False, tier:free}, so the agent saw a silent downgrade
            # and never learned that binding restores the key. Surface it.
            if _reason == "bind_email_required":
                return jsonify({
                    "valid":  False,
                    "tier":   "free",
                    "reason": _reason,
                    "upgrade_hint": (
                        "This DC Hub key used its 10 free unbound calls. It keeps "
                        "working FREE (50 calls/day) the moment it's bound to your "
                        "operator's email — call the bind_email tool, or POST "
                        "/api/v1/keys/auto-trial/bind {api_key, email}. PAUSE and "
                        "ask your human: 'DC Hub needs an email to keep the free "
                        "tier — which address should I use?' Never invent one."),
                }), 200
        except Exception:
            pass
        # ── 2026-06-10: paid REST/dashboard key used directly at the MCP ──
        # A web-signup founding/pro/enterprise customer points an agent at the
        # MCP using their dashboard api_keys key (dchub_…). That key has NO
        # mcp_dev_keys row, so the lookup above missed it and we were about to
        # return 'free' — the SAME pay→free leak r77 fixed for keys that
        # ALREADY had an mcp_dev_keys row, but never for a brand-new web signup.
        # Resolve the key against api_keys (by key_hash, like mcp_gatekeeper) +
        # users.plan and grant the real tier. Fail-soft → original free return.
        try:
            import hashlib as _hl
            _kh = _hl.sha256(api_key.encode()).hexdigest()
            with _pool.connection() as conn, conn.cursor() as cur:
                # 2026-06-12: (1) match BOTH key_hash conventions — standard keys
                # store sha256(api_key), partner/admin keys (incl. the owner's
                # own enterprise key) store the RAW api_key string; a hash-only
                # match missed them. (2) api_keys.is_active is an INTEGER column,
                # so `IN (1, TRUE)` threw "operator does not exist: integer =
                # boolean" → the whole fallback silently failed and returned
                # free for every web-signup paid customer using a dashboard key.
                cur.execute(
                    "SELECT ak.rate_limit_tier, ak.plan, u.plan, u.email "
                    "FROM api_keys ak LEFT JOIN users u ON u.id = ak.user_id "
                    "WHERE ak.key_hash IN (%s, %s) "
                    "AND (ak.is_active IS NULL OR ak.is_active = 1) "
                    "LIMIT 1",
                    (_kh, api_key),
                )
                _akr = cur.fetchone()
            if _akr:
                _nt = _node_tier_max([_akr[0], _akr[1], _akr[2]])
                if _nt in ("paid", "enterprise"):
                    return jsonify({
                        "valid":        True,
                        "tier":         _nt,
                        "developer_id": None,
                        "email":        _akr[3],
                        "tier_source":  "api_keys_no_mcp_row",
                        "tier_detail":  {"api_key_tier": _akr[0],
                                         "api_key_plan": _akr[1],
                                         "users_plan":   _akr[2],
                                         "effective":    _nt},
                    }), 200
        except Exception:
            pass
        return jsonify({"valid": False, "tier": "free"}), 200

    # ── 2026-07-10 (funnel audit, leak #2): the cumulative 10-call bind gate
    # only lived in validate_trial_key, so dch_trial_ keys were forced to bind
    # while the dch_live_ keys claim_free_key mints (no email, no expiry) never
    # were — the busiest anonymous cohort had NO forcing function at all.
    # Extend the SAME gate here: an UNBOUND dch_live_ key gets
    # TRIAL_FREE_CALLS_UNBOUND cumulative validated calls, then drops to the
    # bind_email_required response (identical shape to the trial gate — the
    # Node side already surfaces reason/upgrade_hint, e918eaa). Binding via
    # /api/v1/keys/identify sets mcp_dev_keys.email, which lifts the gate on
    # the next validate. Counter lives in metadata->>'validate_calls' (no DDL;
    # only unbound dch_live_ rows are ever touched). FAIL-OPEN: any error falls
    # back to the legacy last_used_at-only update and no gate.
    _unbound_live = (api_key.startswith("dch_live_")
                     and not (row[1] or "").strip()
                     and (row[2] or "free").lower() in ("free", "identified"))
    _gated_unbound = False
    if _unbound_live:
        try:
            from routes.auto_trial import TRIAL_FREE_CALLS_UNBOUND as _BIND_GATE_CALLS
        except Exception:
            _BIND_GATE_CALLS = int(os.environ.get("TRIAL_FREE_CALLS_UNBOUND", "10") or 10)
        try:
            with _pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE mcp_dev_keys
                          SET last_used_at = NOW(),
                              metadata = COALESCE(metadata, '{}'::jsonb)
                                         || jsonb_build_object('validate_calls',
                                              COALESCE((metadata->>'validate_calls')::int, 0) + 1)
                        WHERE api_key = %s
                    RETURNING (metadata->>'validate_calls')::int""",
                    (api_key,),
                )
                _vr = cur.fetchone()
            # Mirror the trial semantics: the first N calls pass (prior count
            # 0..N-1), call N+1 is gated. RETURNING gives the post-increment
            # value, so gate when it EXCEEDS the allowance.
            _gated_unbound = bool(_vr) and int(_vr[0] or 0) > _BIND_GATE_CALLS
        except Exception:
            try:
                with _pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                        (api_key,),
                    )
            except Exception:
                note_swallowed_write("mcp_dev_keys", where="flask_mcp_endpoints.validate_key")
                pass
    else:
        try:
            with _pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                    (api_key,),
                )
        except Exception:
            note_swallowed_write("mcp_dev_keys", where="flask_mcp_endpoints.validate_key")
            pass
    if _gated_unbound:
        return jsonify({
            "valid":  False,
            "tier":   "free",
            "reason": "bind_email_required",
            "upgrade_hint": (
                f"This DC Hub key used its {_BIND_GATE_CALLS} free unbound calls. "
                "It keeps working FREE (50 calls/day) the moment it's bound to your "
                "operator's email — call the bind_email tool, or POST "
                "/api/v1/keys/identify {api_key, email}. PAUSE and ask your human: "
                "'DC Hub needs an email to keep the free tier — which address "
                "should I use?' Never invent one. Re-claiming a fresh key won't "
                "reset this — the counter follows your identity."),
        }), 200

    # ── 2026-06-07: tier-table-gap cross-check (SITE_QA.md bug #2) ──
    # mcp_dev_keys.tier can drift below users.plan / api_keys.rate_limit_tier
    # when Stripe webhook upgrades the user but the mcp_dev_keys backfill
    # didn't run (the r77 backfill healed 14 customers, but new churn keeps
    # producing drift). Take the HIGHEST tier across all 3 tables so a paying
    # customer never sees current_tier='free' just because mcp_dev_keys lags.
    mcp_tier = (row[2] or "free").lower()
    user_email = row[1]
    plan_tier = None
    api_key_tier = None
    _metered_over = False
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Both other tier sources + the metered flag, in one place —
            # see _tier_cross_check above. Extracted so the monthly-quota
            # gate resolves tiers the SAME way instead of re-deriving them.
            plan_tier, api_key_tier, _metered_over = _tier_cross_check(
                cur, api_key, user_email, want_metered=True)
    except Exception:
        # fail-soft: stick with mcp_dev_keys.tier if cross-check fails
        pass

    # Tier ranking — pick the highest across all sources.
    _RANK = {
        "anonymous": -1, "anon": -1, "free": 0, "identified": 1,
        "starter": 2, "developer": 3, "founding": 4, "pro": 4,
        "team": 5, "metered": 5, "enterprise": 6, "research_seed": 6,
        "admin": 99,
    }
    candidates = [mcp_tier]
    if plan_tier:
        candidates.append(plan_tier)
    if api_key_tier:
        candidates.append(api_key_tier)
    # Normalize to the Node gate's vocabulary (server.mjs unlocks only
    # 'paid'/'enterprise'); founding/pro/team/metered all map to 'paid'.
    # Previously this returned the raw plan name (e.g. 'founding'), which the
    # Node gate did not recognize → a founding/pro customer with an
    # mcp_dev_keys row was silently gated to the paywall.
    effective_tier = _node_tier_max(candidates)

    # If we promoted, log it (helps the L23 billing-drift detector spot
    # keys that need an mcp_dev_keys.tier backfill).
    if effective_tier != mcp_tier:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "[keys.validate] tier promoted: api_key=%s mcp_dev_keys=%s plan=%s api_key_tier=%s → %s",
                api_key[:20] + "...", mcp_tier, plan_tier, api_key_tier, effective_tier,
            )
        except Exception:
            pass

    # r-metered-enforce: only ever enforce a FREE-tier identity — a key that has
    # since upgraded (paid) must never be blocked. Dark unless the env switch is on.
    metered_enforce = bool(_metered_over and _RANK.get(effective_tier, 0) <= 1)

    # r-streak (2026-07-18): free/identified keys get the return-streak state on
    # every validate — the progressive daily-cap unlock only pulls agents back
    # if they can SEE the streak. Paid tiers don't need it. Fail-open: None.
    _streak = None
    if _RANK.get(effective_tier, 0) <= 1:
        try:
            from return_streak import streak_snapshot
            _streak = streak_snapshot(api_key)
        except Exception:
            _streak = None

    return jsonify({
        "valid":        True,
        "tier":         effective_tier,
        "developer_id": row[0],
        "email":        row[1],
        "streak":       _streak,
        "metered_enforce": metered_enforce,
        "tier_source":  "highest_of_3" if effective_tier != mcp_tier else "mcp_dev_keys",
        "tier_detail":  {
            "mcp_dev_keys": mcp_tier,
            "users_plan":   plan_tier,
            "api_key_tier": api_key_tier,
            "effective":    effective_tier,
        },
    }), 200


# ── GET /api/v1/mcp/usage-today ────────────────────────────────────────────
# Phase 274: per-key per-tool daily usage so server.mjs can enforce daily
# caps on specific free-tier tools (e.g. get_grid_intelligence, get_fiber_intel
# at 10/day each). Counts only successful (status='ok') calls so blocked or
# errored attempts don't burn through the user's quota.
#
# Internal-only (X-Internal-Key) because exposing real call counts publicly
# would let a free user infer when others are competing for shared quota.
#
# Fail-soft contract: if anything goes wrong (table missing, DB blip, bad
# input), return count=0. Caller (server.mjs) defaults to allowing the call
# on the assumption that quota is intact — losing one billable enforcement
# event is preferable to breaking the user's tool call over a transient bug.

@mcp_bp.get("/api/v1/mcp/usage-today")
@_require_internal
def mcp_usage_today():
    api_key = (request.args.get("api_key") or "").strip()
    tool    = (request.args.get("tool") or "").strip()
    if not api_key or not tool:
        return jsonify({"count": 0, "error": "api_key and tool required"}), 200
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)::int
                     FROM mcp_call_log
                    WHERE api_key   = %s
                      AND tool      = %s
                      AND status    = 'ok'
                      AND timestamp >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')""",
                (api_key, tool),
            )
            n = (cur.fetchone() or [0])[0]
        return jsonify({
            "count": int(n or 0),
            "tool": tool,
            "as_of": "today_utc",
        }), 200
    except Exception as e:
        # Fail-soft: return 0 so the caller doesn't accidentally block
        # legitimate users on a transient DB error.
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("mcp_usage_today error: %s", e)
        except Exception:
            pass
        return jsonify({"count": 0, "error": str(e)[:160], "fail_soft": True}), 200


# ── GET /api/v1/mcp/monthly-usage ──────────────────────────────────────────
# Monthly-quota surface: the per-key month rollup written by track_tool_call,
# reported against the tier's monthly quota (mcp_daily x 30), PLUS the
# phase-2 enforcement decision.
#
# Phase 2 (2026-08-06) added `allowed` / `blocked` / `message` /
# `upgrade_url`. The decision is inert until MONTHLY_QUOTA_ENFORCE=1: while
# the switch is off an over-quota key reports reason=over_quota_log_only and
# allowed stays true, which is exactly what the log-only review window reads.
# The gateway is expected to consult `allowed` and, when it is false, serve
# `message` through the MCP error channel (content on that path IS the error
# channel — see monthly_quota._wall_message).
#
# ★ TIER RESOLUTION. `tier` here arrives from the caller in the NODE gate's
# vocabulary, where 'paid' means founding/pro/team/metered. There is no
# 'paid' key in TIER_LIMITS, so trusting it naively would resolve every
# paying customer to the free 300/month fallback. We therefore take the
# BEST of (a) the tier the caller passed and (b) the tier resolved
# server-side the same highest-of-3 way validate_key does, and route both
# through monthly_quota.resolve_quota_tier. An unresolvable tier fails open.
#
# Internal-only + fail-soft for the same reasons as usage-today above.

def _best_quota_tier(passed_tier, api_key):
    """Highest-quota resolution of a caller's tier, or None to fail open.

    Considers the tier the gateway passed AND the server-side highest-of-3
    lookup, so neither a stale gateway value nor an mcp_dev_keys lag can
    strand a paying customer on the free quota.
    """
    from monthly_quota import monthly_quota_for, resolve_quota_tier

    candidates = []
    for t in (passed_tier, resolve_effective_node_tier(api_key)):
        r = resolve_quota_tier(t) if t else None
        if r:
            candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=monthly_quota_for)


@mcp_bp.get("/api/v1/mcp/monthly-usage")
@_require_internal
def mcp_monthly_usage():
    api_key = (request.args.get("api_key") or "").strip()
    tier    = (request.args.get("tier") or "").strip().lower()
    if not api_key:
        return jsonify({"used": 0, "allowed": True, "blocked": False,
                        "error": "api_key required"}), 200
    try:
        from monthly_quota import month_usage, quota_decision, record_wall_hit
        resolved = _best_quota_tier(tier, api_key)
        with _pool.connection() as conn, conn.cursor() as cur:
            decision = quota_decision(cur, api_key, resolved, ts=None)
            # r-wall-metrics (2026-08-10): an at-quota decision is the
            # conversion event the wall exists to produce — roll it up
            # (mcp_quota_wall_hits) where it is computed so the funnel can
            # see the wall firing. Fail-soft on an autocommit connection: a
            # metrics write must never change or break the decision served.
            if decision.get("would_block"):
                try:
                    record_wall_hit(cur, api_key, decision)
                except Exception as _we:
                    try:
                        import logging as _lg
                        _lg.getLogger(__name__).warning(
                            "record_wall_hit failed (decision unaffected): %s", _we)
                    except Exception:
                        pass
            if decision.get("reason") in ("exempt", "unresolved_tier"):
                # quota_decision returns before reading the rollup on those
                # fail-open paths. The counting rail is the whole point of
                # this endpoint during the log-only window, so still report
                # `used` — just with no quota to compare it against, rather
                # than publishing the misleading free-fallback 300.
                try:
                    decision["used"] = month_usage(cur, api_key)
                except Exception:
                    pass
        # Report the tier the CALLER sent alongside what we resolved it to,
        # so a gateway/backend disagreement is visible in the response
        # rather than only in the block that follows from it.
        out = {**decision,
               "tier": resolved or tier or "free",
               "tier_requested": tier or None}
        return jsonify(out), 200
    except Exception as e:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("mcp_monthly_usage error: %s", e)
        except Exception:
            pass
        # Fail open, loudly: an infrastructure failure must never block.
        return jsonify({"used": 0, "allowed": True, "blocked": False,
                        "reason": "db_error", "error": str(e)[:160],
                        "fail_soft": True}), 200


# ── r-pack5: $5/1000 prepaid-credit balance + burn (gateway-facing) ─────────
# Internal-only (X-Internal-Key). The MCP gateway calls balance before serving a
# gated flagship tool to a non-paid caller, and burn after. Both fail-soft so a
# DB blip can never break a tool call — the gateway falls back to the free-taste
# path. Matched on the durable api_key OR the buying mcp session (same-session
# instant unlock). Reuses routes.mcp_conversion_plays (the mcp_topups store).
@mcp_bp.get("/api/v1/mcp/credits/balance")
@_require_internal
def mcp_credits_balance():
    api_key = (request.args.get("key") or request.args.get("api_key") or "").strip()
    session = (request.args.get("session") or request.args.get("mcp_session") or "").strip()
    try:
        from routes.mcp_conversion_plays import get_credit_status
        st = get_credit_status(api_key or None, session or None)
        credits = int(st.get("credits") or 0)
        # had_pack = ever bought a pack (even if depleted) → gateway shows a
        # "top up $5" re-up nudge to a proven buyer instead of claim-free-key.
        return jsonify({"credits": credits, "has_pack": credits > 0,
                        "had_pack": bool(st.get("had_pack"))}), 200
    except Exception as e:
        return jsonify({"credits": 0, "had_pack": False, "error": str(e)[:160],
                        "fail_soft": True}), 200


@mcp_bp.post("/api/v1/mcp/credits/burn")
@_require_internal
def mcp_credits_burn():
    body = request.get_json(silent=True) or {}
    api_key = (body.get("key") or body.get("api_key") or "").strip()
    session = (body.get("session") or body.get("mcp_session") or "").strip()
    try:
        cost = max(1, int(body.get("cost") or 1))
    except Exception:
        cost = 1
    try:
        from routes.mcp_conversion_plays import consume_credits
        r = consume_credits(api_key or None, session or None, cost)
        return jsonify({
            "ok": bool(r.get("ok")),
            "remaining": int(r.get("remaining") or 0),
            "burned": r.get("burned"),
            "tool": body.get("tool"),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "remaining": 0, "error": str(e)[:160],
                        "fail_soft": True}), 200


# ── POST /api/v1/keys/claim ────────────────────────────────────────────────
# Phase 275: programmatic dev-key claim — no email verification required.
#
# Why this exists
# ---------------
# The disruption audit confirmed: AI agents (Claude in IDE, Cursor, Cline,
# autonomous agents) cannot complete the existing redeem flow because it
# requires a human to verify an email. So the practical anonymous → key
# conversion path is broken for the audience your funnel is *aimed at*.
#
# This endpoint creates a free-tier dev key with one POST. The trade is:
#   • No email = no humanity proof = IP-based rate limit instead (1/24h)
#   • Marked metadata.source='claim_api', metadata.unverified=true so
#     abusive cohorts can be bulk-revoked by source filter later
#   • Same 100/day quota as email-verified free tier; same 10/day cap on
#     grid_intelligence + fiber_intel (phase 274)
#   • Same paid-only walls on analyze_site, compare_sites, etc.
#
# Net effect: an AI agent can claim a key in one curl, use the free tier
# immediately, and (if its human operator wants more) upgrade to Pro via
# Stripe later. Email verification becomes an optional upgrade path
# ("verify to lift the per-IP rate limit") instead of a hard gate.

import re as _kc_re


def _restamp_claim_session(api_key: str) -> None:
    """Re-point a REUSED claim key at the session claiming it right now.

    brain-ascension #28 wave 3 (2026-07-25) — the durable-identity carry fix.
    Both reuse branches below return an existing key WITHOUT touching its
    metadata, and DCHUB_CLAIM_REUSE_HOURS defaults to 720h/30d, so most repeat
    claims returned a key still stamped with a session that died weeks ago.
    Three things broke at once off that one stale field:
      1. flywheel `ret_claim_carry` joins mcp_call_log.session_id to
         metadata->>'session_id' — a stale stamp drops the key out of the
         denominator entirely (the measured 30/60 = 50%).
      2. _resolve_session_claimed_key (above) can never resolve the live
         session, so post-claim calls stay attributed to the anon identity.
      3. trial_check's session_api_key handoff returns nothing, so server.mjs
         never binds sessionMeta — the agent keeps calling as anonymous.
    Re-stamping makes the CURRENT session the owner, which is the true state:
    this session just claimed this key. Fail-soft and best-effort — a stamp
    error must never break a claim (better an unstamped key than a 500).
    """
    sid = (request.headers.get("X-MCP-Session") or "").strip()[:200]
    if not sid or not api_key:
        return
    conn = None
    try:
        # Same autocommit connection helper track_tool_call uses — this module
        # has no module-level get_db, and an autocommit conn keeps the stamp
        # independent of any surrounding aborted transaction.
        conn = _open_track_conn()
        with conn.cursor() as cur:
            # Stamp session_id AND session_bound_at together. The bind time is
            # load-bearing: the /track reconcile sweep's only temporal guard
            # was `k.created_at <= l.timestamp`, which bounded the back-fill
            # correctly ONLY while session_id was write-once at mint. Re-stamping
            # decouples the two by up to the 30d reuse window, so without a bind
            # timestamp the sweep would attribute this session's PRE-claim
            # anonymous calls to the key — inflating the very carry/activation
            # metric this fix exists to move. Never let a fix fake its own proof.
            cur.execute(
                """UPDATE mcp_dev_keys
                      SET metadata = jsonb_set(
                            jsonb_set(COALESCE(metadata, '{}'::jsonb),
                                      '{session_id}', to_jsonb(%s::text), true),
                            '{session_bound_at}',
                            to_jsonb(to_char(NOW() AT TIME ZONE 'UTC',
                                     'YYYY-MM-DD"T"HH24:MI:SS"Z"')), true)
                    WHERE api_key = %s
                      AND COALESCE(metadata->>'session_id', '') <> %s""",
                (sid, api_key, sid))
        # Drop the memo so the very next call resolves the new owner instead
        # of a cached miss from before this claim.
        _SESSION_CLAIMED_KEY_CACHE.pop(sid, None)
    except Exception as e:  # noqa: BLE001 — never break a claim on a stamp
        try:
            import logging as _lg
            _lg.getLogger(__name__).debug(
                "claim session re-stamp skipped: %s", str(e)[:120])
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _inherit_paid_tier(cur, api_key, email):
    """Lift an MCP key to the tier its owner has ALREADY paid for.

    r-coldbuy (2026-08-08): a customer who pays BEFORE ever claiming a key
    — the Stripe payment-link path, where there is no mcp_dev_keys row to
    match on — left the checkout webhook's email UPDATE at rows=0, so the
    MCP surface they just bought stayed free. identify_key has carried this
    inheritance since r77, but claim_key never did, and
    `claim_free_key({"email": ...})` is the FIRST thing an agent calls. So
    the natural pay-then-connect order landed on the one path that could
    not upgrade. Same SQL, one definition, called from both.

    Guarded to only ever raise a stuck key (never demote), and best-effort:
    a failure here must not break a claim or a bind.
    """
    if not email:
        return 0
    try:
        cur.execute(
            """UPDATE mcp_dev_keys AS k
                   SET tier = CASE WHEN u.plan = 'enterprise'
                                   THEN 'enterprise' ELSE 'paid' END
                  FROM users u
                 WHERE k.api_key = %s
                   AND LOWER(u.email) = LOWER(%s)
                   AND u.plan IN ('developer','pro','founding','enterprise')
                   AND COALESCE(u.subscription_status,'') = 'active'
                   AND COALESCE(k.tier,'free') NOT IN ('paid','enterprise')""",
            (api_key, email),
        )
        _rows = cur.rowcount or 0
        if _rows:
            print(f"🔑 Inherited paid tier for {email} on claim/bind (rows={_rows})")
        return _rows
    except Exception as _e:  # noqa: BLE001 — never break a claim/bind
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "paid-tier inheritance skipped for %s: %s", email, str(_e)[:120])
        except Exception:
            pass
        return 0


@mcp_bp.post("/api/v1/keys/claim")
def claim_key():
    """Public: claim a free dev key without email. Rate-limited by IP.

    Body (all optional, used for telemetry only):
        {"client_name": "claude-code", "intended_use": "score build sites"}

    Returns 200 with api_key on success, 429 if this IP already claimed
    one in the last 24h. Never returns an error that requires a retry
    decision — if anything backend-side fails, returns 503 with a
    short human-readable hint pointing at the email-verified path.
    """
    body = request.get_json(silent=True) or {}
    client_name = (str(body.get("client_name") or ""))[:80]
    intended_use = (str(body.get("intended_use") or ""))[:400]
    # Phase FF (2026-05-22): OPTIONAL email capture. Turns claimed keys into
    # addressable contacts (the visitor-intel "0 known email" → a real nurture
    # list + unblocks /admin/upgrade-pool/backfill-emails). Frictionless: omit
    # it and you still get a key instantly. Purely identity capture — does NOT
    # touch gating or daily limits (that stays in the gatekeeper).
    email = (str(body.get("email") or "")).strip().lower()[:200]
    if email and not _kc_re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        email = ""  # invalid → ignore, still mint the anonymous key

    # Real IP behind Cloudflare / Railway proxy. 2026-07-10: prefer
    # CF-Connecting-IP (the SAME resolution routes/auto_trial.py uses) — on
    # the direct-REST path through the CF zone worker, XFF's first hop is the
    # worker's ROTATING egress (verified live: two same-client_name claims 12s
    # apart stored 104.23.187.147 vs 104.23.190.144), so the (client_name, ip)
    # dedupe and the gated-identity/carry checks could never match two
    # requests from the same caller. Aligning with auto_trial also makes the
    # cross-system counter carry (sha256(ip) hash vs metadata->>'ip') match
    # the same caller across both key tables. The MCP-server path sends no CF
    # header and falls back to XFF, whose first hop it already sets to the
    # real caller IP (r-durable-key 2026-07-06).
    ip = (request.headers.get("CF-Connecting-IP", "").strip()
          or request.headers.get("X-Forwarded-For", request.remote_addr or "")
          .split(",")[0].strip())[:64]
    ua = (request.headers.get("User-Agent") or "")[:300]

    # Cheap sanity check on the source IP — should look like an IP
    if ip and not _kc_re.match(r"^[\d:.]{3,45}$", ip):
        ip = ip[:64]  # keep but flag in metadata

    # Phase ZZ+1 (2026-05-15) — DEDUPE STRATEGY CHANGE.
    #
    # Was: 1 key per IP per 24h. Silently broke shared-IP deployments
    # (CI/CD runners, corporate proxies, containerized agents). A single
    # Docker image deployed across 10 GitHub Actions runners would claim
    # once and then 9 sibling agents got 429s — a major reason the
    # claim-rate dropped from 12/week to 2/week.
    #
    # Now: dedupe by (client_name, ip) tuple. If the SAME client_name
    # from the SAME IP already claimed within 24h, return the existing
    # key (idempotent — avoids key proliferation). If a DIFFERENT
    # client_name claims from the same IP, that's a new agent, mint a
    # new key. Anonymous claims (no client_name) still fall back to IP
    # dedup to prevent random-bot key flooding.
    #
    # r-durable-key (2026-07-06): the reuse window was a hard-coded 24h, so a
    # returning agent the NEXT day always missed the dedupe and minted a fresh
    # dch_live_ key — mature key-reuse stuck ~1.7% (retention is a cross-week
    # signal; a sub-day window structurally can't produce it). Widen to an
    # env-tunable default of 30d (720h). Pairs with the MCP server now forwarding
    # the real caller IP (X-Forwarded-For) so metadata->>'ip' is the agent, not
    # the shared proxy egress.
    try:
        _reuse_hours = max(1, int(os.environ.get("DCHUB_CLAIM_REUSE_HOURS", "720")))
    except Exception:
        _reuse_hours = 720

    # ── 2026-07-10 (funnel audit, leak #1: RE-MINT ESCAPE) ──────────────────
    # A key gated at TRIAL_FREE_CALLS_UNBOUND cumulative unbound calls could
    # simply call claim_free_key again (with a different client_name — the
    # response even suggested it) and get a fresh anonymous dch_live_ key with
    # a reset counter, making re-minting strictly cheaper than binding. Close
    # it: if this IP already holds a GATED unbound claim key (ANY client_name,
    # ANY UA — the per-tuple dedupe below is exactly the hole), return the SAME
    # gated key with the bind CTA instead of minting. FAIL-OPEN on any error.
    try:
        from routes.auto_trial import TRIAL_FREE_CALLS_UNBOUND as _BIND_GATE_CALLS
    except Exception:
        _BIND_GATE_CALLS = int(os.environ.get("TRIAL_FREE_CALLS_UNBOUND", "10") or 10)
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT api_key, tier,
                          COALESCE((metadata->>'validate_calls')::int, 0)
                     FROM mcp_dev_keys
                    WHERE metadata->>'source' = 'claim_api'
                      AND metadata->>'ip' = %s
                      AND (email IS NULL OR email = '')
                      AND status = 'active'
                      AND COALESCE((metadata->>'validate_calls')::int, 0) >= %s
                      AND created_at > NOW() - make_interval(hours => %s)
                    ORDER BY created_at DESC
                    LIMIT 1""",
                (ip, _BIND_GATE_CALLS, _reuse_hours),
            )
            _gated = cur.fetchone()
        if _gated:
            _restamp_claim_session(_gated[0])
            return jsonify(
                ok=True,
                api_key=_gated[0],
                tier=(_gated[1] or "free"),
                reused=True,
                bind_required=True,
                gate="bind_email_required",
                free_calls_unbound=_BIND_GATE_CALLS,
                bind_endpoint="https://dchub.cloud/api/v1/keys/identify",
                note=(f"This identity already used its {_BIND_GATE_CALLS} free "
                      f"unbound calls, so re-claiming returns the SAME key — a "
                      f"fresh mint would not reset the counter. The key keeps "
                      f"working FREE the moment it's bound to your operator's "
                      f"email: call the bind_email tool, or POST "
                      f"/api/v1/keys/identify {{api_key, email}}. Ask your "
                      f"human for the address — never invent one."),
            ), 200
    except Exception as e:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("claim_key gated-identity check failed: %s", e)
        except Exception:
            pass

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            if client_name:
                # New path: (client_name + ip) tuple — preserves
                # multi-agent shared-IP deployments
                cur.execute(
                    """SELECT created_at, api_key, tier
                         FROM mcp_dev_keys
                        WHERE metadata->>'source' = 'claim_api'
                          AND metadata->>'client_name' = %s
                          AND metadata->>'ip' = %s
                          AND created_at > NOW() - make_interval(hours => %s)
                        ORDER BY created_at DESC
                        LIMIT 1""",
                    (client_name, ip, _reuse_hours),
                )
            else:
                # Legacy path for anonymous (no client_name) claims —
                # IP-only dedup, same 24h window
                cur.execute(
                    """SELECT created_at, api_key, tier
                         FROM mcp_dev_keys
                        WHERE metadata->>'source' = 'claim_api'
                          AND metadata->>'ip' = %s
                          AND (metadata->>'client_name' IS NULL
                               OR metadata->>'client_name' = '')
                          AND created_at > NOW() - make_interval(hours => %s)
                        ORDER BY created_at DESC
                        LIMIT 1""",
                    (ip, _reuse_hours),
                )
            existing = cur.fetchone()
        if existing:
            # Idempotent: if the SAME client_name (or same anon IP)
            # claimed recently, return the existing key instead of 429.
            # Agents that lost track of their key get it back; agents
            # restarted in CI/CD pipelines reuse their slot. No more
            # silent 429 walls.
            existing_at, existing_key = existing[0], existing[1]
            # Echo the key's ACTUAL tier (the reuse SELECT now fetches it). A key
            # minted before the 'identified' carrot is still 'free'; a new claim is
            # 'identified'. Never hardcode — that would over-claim to the agent.
            existing_tier = (existing[2] if len(existing) > 2 and existing[2] else "free")
            _restamp_claim_session(existing_key)
            return jsonify(
                ok=True,
                api_key=existing_key,
                tier=existing_tier,
                daily_calls=(50 if existing_tier == "identified" else 100),
                reused=True,
                note=(f"Existing key reused for client_name='{client_name or '(anon)'}' "
                      f"from this IP within the reuse window. This is idempotent — call "
                      f"again with a different client_name to mint a fresh key for "
                      f"a different agent on the same machine."),
            ), 200
    except Exception as e:
        # If the lookup fails, don't block — claim through (better to
        # accidentally issue an extra key than to break legit users).
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("claim_key dedup-check failed: %s", e)
        except Exception:
            pass

    # ── r-unused-key-cap (2026-09-04): CAP UNUSED KEYS PER IP ──────────────
    # This endpoint PUBLISHES, in its own `rate_limit_note`, that it is
    # "rate-limited to 1 key per IP per 24h". It was not. Reuse is keyed on
    # (client_name, ip) — deliberately, so multi-agent deployments on one host
    # get a key each — and `client_name` is caller-supplied and unvalidated.
    # Incrementing a string therefore minted unlimited keys from one address.
    #
    # ★ SOMEBODY CHECKED. On 2026-09-01, 4.43.13.119 minted twelve keys in
    # twenty-five minutes as client_name `pentest`, `pentest2` … `pentest12`,
    # and used NONE of them. That is a rate-limit probe, it succeeded, and the
    # published contract said it could not happen.
    #
    # The multi-agent feature is real and must survive: in the same 30 days
    # 152.55.177.123 minted 11 keys and used 11, 152.55.176.168 minted 10 and
    # used 10, 152.55.176.25 minted 8 and used 8. A flat per-IP key cap would
    # break exactly the users the feature exists for.
    #
    # So cap the UNUSED ones. A legitimate deployment mints a key and calls
    # with it; across every repeat claimer in the window, the ones that used
    # their keys carried at most ONE unused key at a time. An identity sitting
    # on _UNUSED_KEY_CAP untouched keys does not need a fresh one — it needs
    # the one it already has, so we hand back the newest instead of minting.
    # Nothing is revoked and no legitimate caller is refused; the enumeration
    # just stops being free. Binding an email (free) lifts it, which keeps the
    # cheapest path the one that also identifies the user.
    #
    # FAIL-OPEN, like the dedup check above: a failed count claims through.
    # Better to issue an extra key than to break a real agent.
    _UNUSED_KEY_CAP = max(1, int(os.environ.get("DCHUB_CLAIM_UNUSED_CAP", "3")))
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT api_key, tier, COUNT(*) OVER () AS unused_n
                     FROM mcp_dev_keys
                    WHERE metadata->>'source' = 'claim_api'
                      AND metadata->>'ip' = %s
                      AND (email IS NULL OR email = '')
                      AND status = 'active'
                      AND last_used_at IS NULL
                      AND created_at > NOW() - make_interval(hours => %s)
                    ORDER BY created_at DESC""",
                (ip, _reuse_hours),
            )
            _unused = cur.fetchall()
        if _unused and len(_unused) >= _UNUSED_KEY_CAP:
            _u_key, _u_tier = _unused[0][0], (_unused[0][1] or "free")
            _restamp_claim_session(_u_key)
            return jsonify(
                ok=True,
                api_key=_u_key,
                tier=_u_tier,
                reused=True,
                gate="unused_key_cap",
                unused_keys=len(_unused),
                unused_key_cap=_UNUSED_KEY_CAP,
                bind_endpoint="https://dchub.cloud/api/v1/keys/identify",
                note=(f"This IP already holds {len(_unused)} claimed keys that "
                      f"have never been used, so re-claiming returns the newest "
                      f"one instead of minting another. Use this key — it works "
                      f"and has the full free-tier quota. If you genuinely need "
                      f"separate keys for separate agents, bind an email to this "
                      f"one (free, POST /api/v1/keys/identify) and the cap "
                      f"lifts."),
            ), 200
    except Exception as e:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("claim_key unused-cap check failed: %s", e)
        except Exception:
            pass

    # ── 2026-07-10 (leak #1, part 2): CARRY THE COUNTER FORWARD. Even when no
    # live gated key exists to hand back (different client_name pre-gate, a
    # gated dch_trial_ from the auto-mint door, or an expired/older key), the
    # fresh key inherits the identity's cumulative unbound usage — max across
    # BOTH key systems for this IP inside the reuse window. Rotating
    # client_names or hopping mint doors no longer resets the meter; binding
    # an email (free) is now strictly cheaper than re-minting. FAIL-OPEN → 0.
    _carry_calls = 0
    try:
        import hashlib as _cf_hl
        # auto_trial_keys stores sha256(ip)[:16] (routes/auto_trial.py mint).
        _cf_ip_hash = _cf_hl.sha256(ip.encode()).hexdigest()[:16]
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(MAX(COALESCE((metadata->>'validate_calls')::int, 0)), 0)
                     FROM mcp_dev_keys
                    WHERE metadata->>'source' = 'claim_api'
                      AND metadata->>'ip' = %s
                      AND (email IS NULL OR email = '')
                      AND created_at > NOW() - make_interval(hours => %s)""",
                (ip, _reuse_hours),
            )
            _cf_live = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                """SELECT COALESCE(MAX(COALESCE(call_count, 0)), 0)
                     FROM auto_trial_keys
                    WHERE request_ip_hash = %s
                      AND signed_up_email IS NULL AND operator_email IS NULL
                      AND minted_at > NOW() - make_interval(hours => %s)""",
                (_cf_ip_hash, _reuse_hours),
            )
            _cf_trial = int((cur.fetchone() or [0])[0] or 0)
        _carry_calls = max(_cf_live, _cf_trial)
    except Exception:
        _carry_calls = 0

    # Mint the key
    api_key = "dch_live_" + secrets.token_hex(16)
    developer_id = "dev_" + secrets.token_hex(8)
    claim_id = "clm_" + secrets.token_hex(8)
    metadata = {
        "source": "claim_api",
        "unverified": True,
        "ip": ip,
        "user_agent": ua,
        "client_name": client_name or None,
        "intended_use": intended_use or None,
        "claim_id": claim_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "email_captured": bool(email),
        # keystone (audit item 1, 2026-06-30): bind this key to the calling MCP
        # session so a later same-session call on ANY replica resolves it durably.
        # The in-memory sessionMeta bind was lost across replicas, which is why
        # claim_free_key returned auto_applied_to_session:false and the next call
        # came back _bind-only. trial_check reads this session_id back (below) and
        # hands the key to server.mjs to bind the session for real.
        "session_id": ((request.headers.get("X-MCP-Session") or "").strip()[:200] or None),
    }
    # Only an email-less claim inherits the meter (an email-bound claim is
    # already past the gate). Stamp the provenance so the seed is auditable.
    if _carry_calls > 0 and not email:
        metadata["validate_calls"] = int(_carry_calls)
        metadata["gate_carried_from_identity"] = True

    _claimed_paid = False
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'identified', 'active', %s::jsonb)""",
                (api_key, developer_id, (email or None), json.dumps(metadata)),
            )
            # r-coldbuy (2026-08-08): if this email already bought a plan,
            # the key is born paid. Without this a pay-first customer's very
            # first claim_free_key hands them free-tier depth they're paying
            # to be past. See _inherit_paid_tier.
            _claimed_paid = bool(_inherit_paid_tier(cur, api_key, email))
    except Exception as e:
        return jsonify(
            ok=False,
            error="storage_failed",
            message=(
                f"We couldn't issue a key right now. Try the email-verified "
                f"path at https://dchub.cloud/api/v1/dev-signup ({str(e)[:120]})."
            ),
        ), 503

    # Honest response when the carried meter already trips the gate: the key
    # works, but the very next validate returns bind_email_required — say so
    # up front instead of letting the agent discover a "broken" fresh key.
    _gate_extra = {}
    if (not email) and _carry_calls >= _BIND_GATE_CALLS:
        _gate_extra = {
            "bind_required": True,
            "gate": "bind_email_required",
            "free_calls_unbound": _BIND_GATE_CALLS,
            "note": (f"This identity already used its {_BIND_GATE_CALLS} free "
                     f"unbound calls, and the counter carries onto this key — "
                     f"re-minting does not reset it. Bind your operator's email "
                     f"(free) to keep the free tier: call the bind_email tool, "
                     f"or POST /api/v1/keys/identify {{api_key, email}}."),
        }

    # r-coldbuy (2026-08-08): a key that was just born PAID must not be handed
    # back describing the free tier. Reporting free_tier_summary + an
    # upgrade_url to someone who already paid is exactly the friction that made
    # a paying customer think their subscription hadn't applied.
    _paid_extra = {}
    if _claimed_paid:
        _paid_extra = {
            "paid_plan_applied": True,
            "note": ("This email already has an active paid plan — the key was "
                     "issued at your paid tier, not the free tier."),
        }

    return jsonify(
        ok=True,
        api_key=api_key,
        developer_id=developer_id,
        tier=("paid" if _claimed_paid else "identified"),
        claim_id=claim_id,
        **_gate_extra,
        **_paid_extra,
        unverified=(not email),
        email_captured=bool(email),
        email=(email or None),
        usage_instructions=(
            "Pass this key as X-API-Key header on requests to dchub.cloud/api/v1/* "
            "or in your MCP client config when connecting to dchub.cloud/mcp."
        ),
        # Phase FF (2026-05-22): honest email-capture nudge. If no email was
        # provided, invite one — it saves the key, enables usage alerts +
        # early access to new tools, and (per the funnel plan) is the hook for
        # a future higher daily allowance for verified contacts.
        email_nudge=(None if email else
            "Tip: re-claim with {\"email\": \"you@company.com\"} to save this "
            "key to your account, get usage alerts before you hit the cap, and "
            "early access to new tools."),
        free_tier_summary=(None if _claimed_paid else {
            "daily_calls": 100,
            "daily_caps": {"get_grid_intelligence": 10, "get_fiber_intel": 10},
            "paid_only_tools": ["analyze_site", "compare_sites", "get_dchub_recommendation"],
            # r-streak (2026-07-18): teach the progressive unlock at claim time
            # — the cap grows for keys that come back on distinct days.
            "return_streak": _streak_ladder_text(),
        }),
        # ★ SAY WHAT IS ENFORCED. This read "rate-limited to 1 key per IP
        # per 24h", which was false for as long as reuse was keyed on
        # (client_name, ip): a caller incrementing client_name minted keys
        # without limit, and one did — twelve in twenty-five minutes on
        # 2026-09-01. A published limit nobody enforces is worse than no
        # limit published, because it is the sentence an auditor checks.
        rate_limit_note=(
            ("Email captured — thanks. " if email else
             "This key was claimed without an email. ") +
            "Claims are idempotent per (client_name, IP) within the reuse "
            "window: re-claiming with the same client_name returns the SAME "
            "key, and a different client_name mints a separate key so several "
            "agents can share one host. Unbound keys that are never USED are "
            "capped per IP — past that, claiming returns the newest unused key "
            "instead of a new one. Binding an email lifts the cap."
        ),
        # Phase FF+7 (2026-05-19): point at /upgrade entry-point instead
        # of bare /pricing. /upgrade mints a pair-code on demand and 302s
        # to /redeem/<code> for proper funnel attribution. L14 (Causal
        # Reasoner) identified the bare /pricing redirect as the root
        # cause of paywall_hit→click=0.01% drop-off.
        upgrade_url=(None if _claimed_paid
                     else f"https://dchub.cloud/upgrade?key={api_key}"),
        # Master-shell 2 (2026-06-02): OPTIONAL, opt-in social-proof capture.
        # If the agent's operator wants to share how DC Hub helped, POST that
        # endpoint with {api_key, quote, name?, company?}. Strictly opt-in —
        # ignoring this changes nothing. Stored unapproved (manual admin
        # review) and never exposes the key's email.
        quote_capture_url="https://dchub.cloud/api/v1/keys/claim/quote",
        quote_capture_note=("Optional: if DC Hub helped, share a short quote "
                            "via POST /api/v1/keys/claim/quote {api_key, quote}. "
                            "Opt-in; reviewed by a human before any public use; "
                            "your email is never shown."),
    ), 200


# ── POST /api/v1/keys/identify ─────────────────────────────────────────────
# Phase TT (2026-05-14): value-moment email capture — the missing
# anonymous -> known stage of the funnel.
#
# /keys/claim mints a free key with NO email (frictionless, by design),
# which is great for adoption but leaves 1,558 agents/week completely
# anonymous: nothing to convert, nothing to outreach. This endpoint is
# the capture: once an agent's human shares an email, the agent POSTs
# it here, the email is tied to the key, and the key unlocks a higher
# daily quota. Email-FIRST — no payment ask here. The carrot is "4x
# more free + alerts", which is what actually makes a human do it.
#
# Public + idempotent: re-identifying the same key is a no-op confirm.
# This is the endpoint that finally gives the outreach engine targets
# (_high_intent_targets queries mcp_dev_keys WHERE email IS NOT NULL).

_IDENT_EMAIL_RE = _kc_re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── ai_testimonials quote-capture schema (2026-07-30) ──────────────────────
# The live table carried a broad UNIQUE (platform, context) constraint
# (ai_testimonials_platform_context_unique — live-only: no repo CREATE TABLE
# ever declared it). context holds the COMPANY on the two volunteered-quote
# paths below, so the SECOND quote from the same (platform, company) pair
# violated it: identify swallowed the error (quote_captured=False),
# /keys/claim/quote returned opaque storage_failed. Zero source='claim_quote'
# rows ever landed (verified live 2026-07-30).
#
# The constraint's real job was dedup for the mcp-auto capture writers
# (main.py auto-capture: 1-hour app-side check + bare ON CONFLICT DO NOTHING,
# with context as the dedup key). Keep exactly that and nothing more: a
# partial unique index scoped to the auto sources — the BARE (target-less)
# ON CONFLICT DO NOTHING form arbitrates against partial indexes, so those
# writers are unchanged — then drop the broad constraint so every
# human-volunteered source (claim_quote, probes, seeds, manual adds) is no
# longer capped at one row per (platform, context).
# Order matters: index first, then drop, so auto-dedup never has a gap.
_TESTIMONIAL_QUOTE_SCHEMA_SQL = (
    """CREATE UNIQUE INDEX IF NOT EXISTS ai_testimonials_auto_dedup
           ON ai_testimonials (platform, context)
           WHERE source IN ('mcp-auto', 'auto')""",
    """ALTER TABLE ai_testimonials
           DROP CONSTRAINT IF EXISTS ai_testimonials_platform_context_unique""",
)
_testimonial_quote_schema_done = False


def _ensure_testimonial_quote_schema():
    """Converge the ai_testimonials dedup schema (idempotent, once/process).

    Statements commit one-by-one via the autocommit shim. Runs on the two
    quote-capture paths so any environment converges without a manual
    migration (prod was migrated by hand 2026-07-30; there this is a no-op).
    """
    global _testimonial_quote_schema_done
    if _testimonial_quote_schema_done:
        return
    with _pool.connection() as conn, conn.cursor() as cur:
        for _stmt in _TESTIMONIAL_QUOTE_SCHEMA_SQL:
            cur.execute(_stmt)
    _testimonial_quote_schema_done = True


@mcp_bp.post("/api/v1/keys/identify")
def identify_key():
    """Tie an email to an existing dev key — the value-moment capture.

    Body: {"api_key": "dch_live_...", "email": "user@example.com"}
    Returns 200 with what the email unlocked, 200+ok:false on bad input
    (never an error that forces the agent into a retry decision).
    """
    body = request.get_json(silent=True) or {}
    api_key = (str(body.get("api_key") or "")).strip()
    # keystone / item-7 seam (2026-06-30): the MCP bind_email tool binds "the key
    # already active on this session" and omits api_key from the BODY, but this
    # endpoint only read body.api_key → agents got "Pass the api_key you claimed".
    # Resolve the caller's key from the request when the body doesn't carry it:
    # the X-API-Key header (the keystone binds the session's live key into ctx,
    # forwarded here), else the key this Mcp-Session-Id is bound to
    # (metadata.session_id — the keystone bind). Fail-soft.
    if not api_key:
        api_key = (request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        _sid = (request.headers.get("X-MCP-Session") or "").strip()[:200]
        if _sid:
            try:
                with _pool.connection() as _c, _c.cursor() as _cur:
                    _cur.execute(
                        """SELECT api_key FROM mcp_dev_keys
                           WHERE metadata->>'session_id' = %s AND status = 'active'
                             AND created_at > NOW() - INTERVAL '24 hours'
                           ORDER BY created_at DESC LIMIT 1""",
                        (_sid,))
                    _r = _cur.fetchone()
                    if _r and _r[0]:
                        api_key = str(_r[0]).strip()
            except Exception:
                pass
    email = (str(body.get("email") or "")).strip().lower()
    # Phase 3 (2026-06-18) — explicit marketing opt-in. CONSENT-SAFE DEFAULT:
    # only an EXPLICIT boolean true (or the strings "true"/"1"/"yes") flips
    # marketing_opt_in; absent / false / anything else leaves it the Phase-2
    # default of false, so a captured MCP email is never marketed without a
    # clear yes. This opt-in is what gives the Phase-3 marketing union an
    # actual audience. It never affects the soft-fail contract or the claim path.
    _moi_raw = body.get("marketing_opt_in")
    marketing_opt_in = (_moi_raw is True
                        or (isinstance(_moi_raw, str)
                            and _moi_raw.strip().lower() in ("true", "1", "yes")))

    if not api_key:
        return jsonify(ok=False, error="missing_api_key",
                       message="Pass the api_key you claimed from /api/v1/keys/claim."), 200
    if not email or not _IDENT_EMAIL_RE.match(email) or len(email) > 254:
        return jsonify(ok=False, error="invalid_email",
                       message="Pass a valid email address to identify this key."), 200

    # Deliverability gate (soft): the regex above is a cheap pre-filter; this
    # rejects role accounts / disposable domains / domains with no MX|A so we
    # don't bind a key to an address recovery can never reach. SOFT-FAIL on
    # every axis — a bad email is a 200+ok:false (never an error/retry), and a
    # missing validator module falls back to the regex-only behavior above so
    # identify can't break on it.
    try:
        from routes.email_validation import validate_email
        _ok, _reason, _norm = validate_email(email)
        if not _ok:
            return jsonify(
                ok=False, error="undeliverable_email", reason=_reason,
                message="That email looks undeliverable — the key still works; "
                        "try another to enable recovery."), 200
        email = _norm or email
    except Exception:
        # Validator absent/broke — never block identify on our own check.
        pass

    # r-funnel-identify (2026-06-25): bind_email/identify writes the email onto the
    # KEY tables (mcp_dev_keys/auto_trial_keys), but the handoff funnel's 'identified'
    # stage counts mcp_high_intent_sessions.claim_email — a DIFFERENT table it never
    # joined, so ~16 real binds/30d were captured yet the dashboard read 0. Stamp the
    # high-intent session row too (server.mjs callAPIWrite forwards X-MCP-Session).
    # Fully isolated best-effort side-write — never blocks or alters the identify path.
    try:
        _mcp_sess = (request.headers.get('X-MCP-Session')
                     or request.headers.get('Mcp-Session-Id') or '').strip()
        if _mcp_sess:
            with _pool.connection() as _sc, _sc.cursor() as _scur:
                _scur.execute(
                    "UPDATE mcp_high_intent_sessions SET claim_email = %s "
                    "WHERE mcp_session_id = %s AND (claim_email IS NULL OR claim_email = '')",
                    (email, _mcp_sess))
                # r-lead-bridge (2026-07-07): ALSO stamp mcp_upgrade_signals.user_email
                # for this session — that's the table lost_conversion_outreach reads to
                # reach non-converted high-intent leads. The web capture path reaches it
                # via mcp_email_capture + the backfill, but the AGENT bind_email path
                # (this endpoint) wrote only the KEY tables + the high-intent row, so an
                # agent-bound email was INVISIBLE to the nurture machinery (~1 reachable
                # lead / 603 high-intent sessions). This only makes the lead REACHABLE +
                # measurable; marketing SENDS stay gated by the explicit marketing_opt_in
                # in lost_conversion_outreach (transactional-only binds are never emailed).
                _scur.execute(
                    "UPDATE mcp_upgrade_signals SET user_email = %s "
                    "WHERE session_id = %s AND (user_email IS NULL OR user_email = '')",
                    (email, _mcp_sess))
                _sc.commit()
    except Exception:
        note_swallowed_write("mcp_high_intent_sessions", where="flask_mcp_endpoints.identify_key")
        pass

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
                (api_key,),
            )
            row = cur.fetchone()
            if not row:
                # r78: trial keys live in auto_trial_keys, NOT mcp_dev_keys —
                # yet the MCP mint payload pointed agents at THIS endpoint, so
                # identify failed 100% for the auto-trial cohort (0 of 214
                # activated trial keys ever identified; the whole
                # activated→identified funnel stage read zero by construction).
                # Fall through with bind semantics (operator_email), which
                # also lifts the key's daily cap 15 → 50.
                if api_key.startswith("dch_trial_"):
                    # Consent provenance, same as the mcp_dev_keys branch.
                    # auto_trial_keys has no jsonb metadata column — stamp a
                    # compact JSON consent marker into the existing `notes`
                    # TEXT column (idempotent: only when not already stamped,
                    # so a re-bind never clobbers it). marketing_opt_in:false.
                    _consent_note = json.dumps({
                        "consent_at": datetime.now(timezone.utc).isoformat(),
                        "lawful_basis": "legitimate_interest_transactional",
                        "marketing_opt_in": False,
                        "purpose": "key_recovery_and_receipts",
                        "identify_source": "mcp_value_moment",
                    })
                    # signed_up_email is the column every funnel metric counts
                    # (weekly dividend tracker, funnel_health emails_captured,
                    # flywheel sweeps) — this path wrote only operator_email,
                    # so every successful bind read as 0 downstream.
                    cur.execute(
                        """UPDATE auto_trial_keys
                               SET operator_email = %s,
                                   signed_up_email = COALESCE(NULLIF(signed_up_email, ''), %s),
                                   notes = CASE
                                             WHEN notes IS NULL OR notes NOT LIKE '%%consent_at%%'
                                             THEN %s ELSE notes END
                             WHERE api_key = %s
                         RETURNING expires_at""",
                        (email, email, _consent_note, api_key),
                    )
                    trow = cur.fetchone()
                    if trow:
                        # ★★★ 2026-09-02: THE THIRD BIND PATH NEVER MIRRORED.
                        # routes/auto_trial calls _mirror_trial_to_mcp_dev_keys
                        # from BOTH of its bind endpoints (940 signed_up_email,
                        # 987 operator_email) so a later Stripe payment by this
                        # email flips THIS key's tier — the r88h hands-free
                        # unlock. This endpoint binds the same column and never
                        # called it, so a key bound here got no mcp_dev_keys row
                        # and the webhook had nothing to lift: the agent pays and
                        # its own key stays free. That is the identified->paid
                        # leak r88h was built to close, still open on this path.
                        #
                        # Measured 2026-09-02: of 4 binds after the mirror
                        # shipped (2026-06-30), only 1 has an mcp_dev_keys row.
                        # Same shape as this path's earlier signed_up_email miss
                        # (see the comment above) — one bind path quietly not
                        # doing what the other two do.
                        #
                        # Ordering: the mirror opens its OWN connection and is
                        # internally fail-soft, so it cannot break this bind. It
                        # runs before this block's commit; on a failed commit the
                        # row is still keyed to a key the agent holds and the
                        # INSERT is ON CONFLICT DO UPDATE, so it is idempotent.
                        try:
                            from routes.auto_trial import (
                                _mirror_trial_to_mcp_dev_keys as _mirror)
                            _mirror(api_key, email)
                        except Exception:
                            pass  # never let the mirror affect the bind
                        return jsonify(
                            ok=True, identified=True, key_type="trial",
                            operator_email=email,
                            daily_calls=50,
                            message=("Email bound to this trial key — daily cap "
                                     "raised 15 → 50 calls/day. Convert to a "
                                     "365-day identified key: POST "
                                     "/api/v1/keys/auto-trial/redeem "
                                     "{api_key, email}. One-click upgrade: "
                                     f"https://dchub.cloud/upgrade?key={api_key}"),
                        ), 200
                return jsonify(ok=False, error="unknown_api_key",
                               message="That key isn't recognized. Claim one at /api/v1/keys/claim."), 200
            existing_email, tier, status = row[0], row[1], row[2]
            if status and status != "active":
                return jsonify(ok=False, error="key_inactive",
                               message=f"That key is {status}."), 200

            already = bool(existing_email)
            # Idempotent: re-identifying with the same email is a clean
            # confirm; a different email re-points the key (humans switch
            # accounts — last-write-wins is fine for a free key).
            # Phase 3: marketing_opt_in is parameter-driven now. Consent-safe
            # default is still false (no opt-in → no marketing); only an
            # EXPLICIT yes from the body sets it true and stamps a
            # marketing_opt_in_at timestamp so the consent moment is auditable.
            # Absent / false → we still write marketing_opt_in:false, which IS
            # the consent-safe Phase-2 default. The trailing || %s::jsonb adds
            # marketing_opt_in_at only on a true opt-in ({} is a no-op merge
            # otherwise). Building that extra jsonb in Python keeps the SQL one
            # expression and avoids a second jsonb_build_object.
            _now_iso = datetime.now(timezone.utc).isoformat()
            _consent_extra = {}
            if marketing_opt_in:
                _consent_extra["marketing_opt_in_at"] = _now_iso
            cur.execute(
                """UPDATE mcp_dev_keys
                       SET email = %s,
                           metadata = COALESCE(metadata, '{}'::jsonb)
                                      || jsonb_build_object(
                                           'identified_at', %s::text,
                                           'identify_source', 'mcp_value_moment',
                                           -- Consent provenance (GDPR/CAN-SPAM): we
                                           -- email this address for key recovery +
                                           -- upgrade receipts always; marketing only
                                           -- when marketing_opt_in is explicitly true.
                                           'consent_at', %s::text,
                                           'lawful_basis', 'legitimate_interest_transactional',
                                           'marketing_opt_in', %s::boolean,
                                           'purpose', 'key_recovery_and_receipts')
                                      || %s::jsonb
                     WHERE api_key = %s""",
                (email, _now_iso, _now_iso, marketing_opt_in,
                 json.dumps(_consent_extra), api_key),
            )

            # r77 (2026-06-07): inherit paid tier if this email already belongs to
            # a paying customer (covers the pay-first-then-connect-MCP order). The
            # MCP gate reads mcp_dev_keys.tier, so without this a paid user who
            # identifies their key later would stay free. Only upgrades a stuck key.
            # r-coldbuy (2026-08-08): extracted to _inherit_paid_tier so claim_key
            # runs the identical rule — it used to have none.
            _inherit_paid_tier(cur, api_key, email)
    except Exception as e:
        # Never hard-fail the agent — it can keep using the key.
        return jsonify(ok=False, error="storage_failed",
                       message="Couldn't save that right now — your key still works; try again later.",
                       detail=str(e)[:120]), 200

    # Funnel event: this is the anonymous -> known conversion we couldn't
    # see before. Best-effort.
    try:
        from routes.redeem_tracking import record_funnel_event
        record_funnel_event(
            "email_captured",
            tier=tier or "free", source="mcp_identify",
            user_agent=request.headers.get("User-Agent"),
            ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or ""),
            metadata={"already_identified": already},
        )
    except Exception:
        pass

    # Phase TT Increment 3: nurture — fire-and-forget welcome email.
    # Deduped per-key inside send_identify_welcome, so a re-identify
    # won't re-send. Never blocks the response.
    try:
        from routes.redeem_tracking import send_identify_welcome
        send_identify_welcome(email, api_key)
    except Exception:
        pass

    # Master-shell 2 (2026-06-02): OPTIONAL opt-in quote on identify. If the
    # operator volunteered a quote alongside their email, capture it to
    # ai_testimonials UNAPPROVED (source='claim_quote') — never auto-published,
    # and the email is NOT copied into the testimonial (no PII exposure).
    quote_captured = False
    _quote = (str(body.get("quote") or "")).strip()
    if _quote and len(_quote) >= 15:
        try:
            _ensure_testimonial_quote_schema()
            # Redact any email pasted into the quote so we never store PII.
            _quote_clean = _kc_re.sub(r"[^@\s]+@[^@\s]+\.[^@\s]+", "[redacted]", _quote)[:1500]
            # Defense-in-depth: redact any email pasted into name/company too (PII
            # parity with /api/v1/keys/claim/quote) so this path can't store PII either.
            _name = _kc_re.sub(r"[^@\s]+@[^@\s]+\.[^@\s]+", "[redacted]", str(body.get("name") or "")).strip()[:120]
            _company = _kc_re.sub(r"[^@\s]+@[^@\s]+\.[^@\s]+", "[redacted]", str(body.get("company") or "")).strip()[:160]
            with _pool.connection() as conn, conn.cursor() as cur:
                # Idempotency guard: identify gets retried by agents; an
                # identical resubmitted quote counts as captured without
                # stacking a duplicate pending row. Matching on QUOTE (never
                # context) is deliberate — a second, different quote from the
                # same company must keep landing.
                cur.execute(
                    """SELECT 1 FROM ai_testimonials
                        WHERE source = 'claim_quote' AND platform = 'mcp_agent'
                          AND quote = %s LIMIT 1""",
                    (_quote_clean,),
                )
                if cur.fetchone() is None:
                    # Bare TARGET-LESS ON CONFLICT DO NOTHING (house ingest-
                    # idempotency lint). Inert for claim_quote rows today: the
                    # only unique index on this table is the auto-scoped
                    # partial one, whose predicate can never match
                    # source='claim_quote'. Never name a conflict target here.
                    cur.execute(
                        """INSERT INTO ai_testimonials
                               (platform, agent_name, quote, context, category, source, approved)
                           VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                           ON CONFLICT DO NOTHING""",
                        ("mcp_agent", (_name or None), _quote_clean,
                         (_company or None), "recommendation", "claim_quote"),
                    )
            quote_captured = True
        except Exception:
            # Never fail identify on a capture hiccup — but never invisibly
            # either: this exact bare swallow hid every post-first-quote
            # capture failure while the broad unique constraint was live.
            note_swallowed_write("ai_testimonials",
                                 where="flask_mcp_endpoints.identify_key.quote")
            quote_captured = False

    masked = email
    try:
        _u, _d = email.split("@", 1)
        masked = (_u[:3] + "***@" + _d)
    except Exception:
        pass

    return jsonify(
        ok=True,
        identified=True,
        already_identified=already,
        quote_captured=quote_captured,
        quote_capture_url="https://dchub.cloud/api/v1/keys/claim/quote",
        email_masked=masked,
        unlocked={
            "daily_calls": int(os.environ.get("MCP_IDENTIFIED_DAILY_LIMIT", "100")),
            "previous_daily_calls": int(os.environ.get("MCP_FREE_DAILY_LIMIT", "25")),
            "extras": ["key tied to your account — recoverable from the dashboard",
                       "upgrade receipts + billing land on this email"],
            # r-streak (2026-07-18): identified keys keep climbing too — the
            # return-streak ladder multiplies the daily cap for keys that
            # come back on distinct days.
            "return_streak": _streak_ladder_text(),
        },
        message=("Email already on file — your key is identified."
                 if already else
                 # ★2026-08-06: was "100 calls/day (up from 25)" — BOTH numbers
                 # were inventions. Canon is free 10/day → identified 50/day
                 # (tier_registry.TIER_LIMITS, which the gates actually read),
                 # so this promised an agent 2x the cap it was about to get.
                 # Read from the registry so it cannot drift again. The free
                 # side stays per-DAY: those gates really are daily.
                 f"Identified — this key now gets {_tier_calls_per_day('identified')} "
                 f"calls/day (up from {_tier_calls_per_day('free')}) "
                 "and is tied to your account for recovery + receipts."),
        # PAID tiers are quoted per MONTH — that is the ceiling monthly_quota
        # enforces, and the per-day figure ("1,000/day") was never enforced on
        # the /mcp path AND was the REST rate_limit, not mcp_daily (500).
        upgrade_note=(f"Need {_tier_calls_per_month('developer')} calls/month + full data? "
                      f"Developer plan is {_tier_price_label('developer')} "
                      "at https://dchub.cloud/pricing"),
    ), 200


# ── POST /api/v1/mcp/track ─────────────────────────────────────────────────

@mcp_bp.post("/api/v1/mcp/track")
@_require_internal
def track_tool_call():
    body = request.get_json(silent=True) or {}

    # r-recipe-lifecycle (2026-07-30, Perplexity round-5): recipe/plan
    # lifecycle events ride the same internal-keyed endpoint but are NOT tool
    # calls — dispatch them to recipe_executions BEFORE any call-table write,
    # or they would inflate the very episode metrics they exist to replace.
    # The gateway payload carries no `tool` field on purpose: a backend
    # without this dispatch drops the event harmlessly at the missing-tool
    # return below instead of logging a phantom call (deploy-skew safe).
    if (body.get("event") or "") == "recipe_lifecycle":
        _rl_conn = None
        try:
            from recipe_lifecycle import record_lifecycle_event
            _rl_conn = _open_track_conn()
            # Identity parity with call rows: a keyless event whose session
            # owns a recently-claimed key attributes to that key, exactly as
            # the r-claim-session-bind resolver does for mcp_call_log below —
            # otherwise the same agent-day would split across the two tables.
            _rl_sid = (str(body.get("session_id") or "").strip() or None)
            if (not (body.get("api_key") or "").strip() and _rl_sid
                    and os.environ.get("DCHUB_CLAIM_SESSION_BIND", "1") != "0"):
                _rl_key = _resolve_session_claimed_key(_rl_conn, _rl_sid[:200])
                if _rl_key:
                    body = dict(body)
                    body["api_key"] = _rl_key
            _rl_ok, _rl_err = record_lifecycle_event(_rl_conn, body)
            # Telemetry contract: always 200 — a malformed or premature event
            # (table not applied yet) must never fail the gateway's callback.
            return jsonify({"ok": _rl_ok,
                            **({"error": _rl_err} if _rl_err else {})}), 200
        except Exception as _rl_e:
            return jsonify({"ok": False, "error": str(_rl_e)[:200]}), 200
        finally:
            if _rl_conn is not None:
                try:
                    _rl_conn.close()
                except Exception:
                    pass

    tool = (body.get('tool') or body.get('tool_name'))
    if not tool:
        return jsonify({"ok": False, "error": "missing tool"}), 200

    ts = body.get("timestamp")
    try:
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
    except Exception:
        ts_dt = datetime.now(timezone.utc)

    params = body.get("params")

    # r-question-class (2026-07-27): stamp the SHAPE of the caller's question
    # onto the logged params. Shell #37 lane 4 was RED because we recorded WHAT
    # ran but never WHAT KIND of question was asked, so the GraphRAG demand
    # read had to reverse-engineer intent from raw params by hand
    # (reference_dchub_global_question_demand). Buckets are frozen to that
    # read's taxonomy so the ~October re-read is like-for-like.
    # Pure + fail-soft: classify() never raises and returns None when the call
    # carried no text key, so params is left untouched for the ~99.5% of calls
    # that are typed-param lookups.
    try:
        _qc_src = params
        if isinstance(_qc_src, str):
            _qc_src = json.loads(_qc_src)
        if isinstance(_qc_src, dict):
            from routes._question_class import classify as _classify_q
            _qc = _classify_q(_qc_src)
            if _qc:
                _qc_src = dict(_qc_src)
                _qc_src["question_class"] = _qc
                params = _qc_src
    except Exception:
        pass  # telemetry enrichment — never block or alter the tracked call

    if params is not None and not isinstance(params, str):
        params = json.dumps(params, default=str)

    # ── Phase NN (2026-05-14): attribution recovery ──────────────────────
    # The upstream MCP server (server.mjs) fires this callback WITHOUT
    # forwarding clientInfo, so client_name is almost always 'unknown'
    # and platform is the literal 'mcp' — 98.8% of mcp_tool_calls rows
    # were unattributed. But server.mjs DOES pass session_id, and the
    # /mcp proxy in main.py persists session_id -> (platform, client_name)
    # to mcp_sessions on every `initialize` (where clientInfo.name IS
    # present). Recover real attribution by joining on session_id.
    _r_platform = str(body.get("platform") or "").strip()
    _r_client = str(body.get("client_name") or body.get("client") or "").strip()
    # r44 (2026-05-25): prefer the modern Mcp-Session-Id HTTP header
    # (per MCP transport spec) over the body field. Modern MCP clients
    # send the session identity in headers; older proxies also pass it
    # in body. Take header first, body as fallback. Stable across all
    # tool calls from the same client → unique session count is now a
    # real attribution metric.
    _r_session = (
        request.headers.get("Mcp-Session-Id")
        or request.headers.get("mcp-session-id")
        or request.headers.get("X-Mcp-Session-Id")
        or body.get("session_id")
    )
    _GENERIC = ("", "mcp", "mcp-worker", "unknown", "anonymous")

    # Phase XX (2026-05-16): UUID detection. The funnel showed 5 of the
    # top-20 platform buckets were UUIDs (session_ids leaking into the
    # platform field upstream). UUIDs were NOT in _GENERIC so the recovery
    # below never fired, and we ended with 5 distinct UUID-keyed buckets
    # that should all have been 'claude' or 'chatgpt'. Treat any 36-char
    # UUID-shaped value as generic so the recovery fires for them too.
    import re as _re_uuid
    _UUID_RE = _re_uuid.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def _looks_generic(v: str) -> bool:
        v_lower = v.lower().strip()
        if v_lower in _GENERIC: return True
        if _UUID_RE.match(v_lower): return True
        return False

    # Also normalize: if the incoming platform IS a UUID, blank it before
    # storage so we don't pollute analytics whether or not recovery succeeds.
    _platform_was_uuid = bool(_UUID_RE.match(_r_platform.lower()))
    _client_was_uuid   = bool(_UUID_RE.match(_r_client.lower()))

    # r-poolsat (2026-07-15): reuse ONE fresh connection across this handler's
    # attribution read + mcp_call_log write. /api/v1/mcp/track fires on EVERY
    # MCP tool call (~5,400/day — the busiest DB-touching endpoint) and used to
    # open TWO separate fresh Neon connections per call (this SELECT + the
    # INSERT below), each paying a full TCP+TLS+Neon-pooler handshake. Under the
    # Neon connection ceiling that churn drove the ~6s track latency and starved
    # every other connect (main-pool getconn/validate → "Pool at 60-98% of 80").
    # One connection, closed in the finally around the call_log write below.
    _tc_conn = None
    try:
        _tc_conn = _open_track_conn()
    except Exception:
        _tc_conn = None

    if (_r_session and _tc_conn is not None
            and (_looks_generic(_r_platform) or _looks_generic(_r_client))):
        try:
            with _tc_conn.cursor() as _sc_cur:
                _sc_cur.execute(
                    "SELECT platform, client_name FROM mcp_sessions WHERE session_id = %s",
                    (str(_r_session)[:200],),
                )
                _sc_row = _sc_cur.fetchone()
            if _sc_row:
                if _sc_row[0] and not _looks_generic(_sc_row[0]) \
                        and _looks_generic(_r_platform):
                    _r_platform = _sc_row[0]
                if _sc_row[1] and not _looks_generic(_sc_row[1]) \
                        and _looks_generic(_r_client):
                    _r_client = _sc_row[1]
        except Exception:
            # mcp_sessions may not exist yet, or lookup hiccupped — fall
            # back to whatever the callback gave us. Never block tracking.
            pass

    # Phase XX: if recovery failed AND the original was a UUID, fall back
    # to detecting from the live User-Agent header. Better an honest 'curl'
    # or 'unknown-ua' than a meaningless UUID polluting the analytics table.
    if _platform_was_uuid and _looks_generic(_r_platform):
        ua = (request.headers.get('User-Agent') or '').lower()
        if   'claude'     in ua: _r_platform = 'claude'
        elif 'chatgpt'    in ua or 'openai-mcp' in ua: _r_platform = 'chatgpt'
        elif 'cursor'     in ua: _r_platform = 'cursor'
        elif 'gemini'     in ua: _r_platform = 'gemini'
        elif 'perplexity' in ua: _r_platform = 'perplexity'
        elif 'copilot'    in ua: _r_platform = 'copilot'
        elif 'cline'      in ua: _r_platform = 'cline'
        elif 'windsurf'   in ua: _r_platform = 'windsurf'
        elif 'grok'       in ua: _r_platform = 'grok'
        elif 'mistral'    in ua or 'le chat' in ua or 'lechat' in ua: _r_platform = 'mistral'
        elif 'cohere'     in ua: _r_platform = 'cohere'
        elif 'curl' in ua or 'postman' in ua: _r_platform = 'curl'
        else: _r_platform = 'unknown-ua'
    if _client_was_uuid and _looks_generic(_r_client):
        _r_client = 'unknown'

    # Item 5 SELF-HEAL DIET (2026-06-13): skip the legacy mcp_tool_calls write
    # for our own self-heal / synthetic probe clients. These were ~93% of the
    # table (~33k/wk all tagged dchub-selfheal) and EVERY downstream reader
    # already filters them out — so the rows were pure write-amplification.
    # The MCP tool itself has already executed; dropping this audit row does
    # NOT affect the response or any health check (those don't read it). The
    # canonical mcp_call_log write below still records internal traffic, so
    # probe health remains observable. We test BOTH the recovered identity
    # (_r_platform/_r_client, resolved via mcp_sessions) and the raw payload
    # fields, since the self-heal loop's clientInfo.name surfaces in both.
    _is_synthetic_selfheal = _is_selfheal_synthetic(
        _r_platform, _r_client,
        body.get("platform"), body.get("client_name"), body.get("client"),
    )

    # r-junk-platform (2026-07-04): junk clientInfo self-IDs ('clawith' 3.4k
    # rows/30d, plus 1-char curl/urllib tags 'v'/'p'/'t'/'w'/'c'/'fv') were
    # landing VERBATIM as mcp_tool_calls.platform — no writer slices anything,
    # server.mjs ships unrecognized clientInfo.name through as the platform
    # tag by design and this endpoint stored it faithfully. Normalize to
    # 'dchub-internal' for the mcp_tool_calls INSERT ONLY (below): computed
    # AFTER the self-heal-diet check so skip behavior is unchanged, and NOT
    # applied to the mcp_call_log write so the canonical log keeps the raw
    # tag for probe-health observability. client_name keeps the raw string.
    _platform_clean = _normalize_write_platform(_r_platform)

    # phase9j_dual: also write to legacy mcp_tool_calls so the existing
    # /api/v1/usage and /api/v1/data-freshness queries (which read from
    # that table) reflect activity. The 4/30 rewrite of this file moved
    # writes to mcp_call_log; this dual-write keeps both readable.
    # r84 POOL-LEAK FIX: the close() used to live INSIDE this try, so any
    # throw in the INSERT/commit (a transient error, a schema hiccup) skipped
    # it and the pooled connection leaked until the 60s forced-reclaim. This
    # fires ~5,400×/day (once per MCP tool call), so a burst of insert errors
    # starved the pool → 30s connection-acquire waits on unrelated reads (the
    # "facility query took 30.8s" + "forced reclaim held 75s by track_tool_call"
    # log lines). Now the connection is ALWAYS returned in finally.
    _db_lt = None
    try:
        from db_utils import try_get_db
        # Item 5: self-heal DIET — never open a connection / write a row for
        # our own synthetic probe loop. This is the ~93% cut.
        _db_lt = try_get_db() if not _is_synthetic_selfheal else None
        if _db_lt:
            _c_lt = _db_lt.cursor()
            _params_str = params if isinstance(params, str) else (json.dumps(params or {}) if params is not None else '{}')
            # Phase FF++ (2026-05-12): DROPPED the session_id fallback in
            # client_name. Previously, when upstream MCP server (server.mjs)
            # didn't pass client_name (which was always — it didn't
            # capture clientInfo.name from the initialize handshake), this
            # line fell back to body.session_id, which is the MCP
            # transport's auto-generated UUID. That polluted every row in
            # mcp_tool_calls with anonymous UUIDs and made vendor
            # detection impossible.
            #
            # Now: prefer real client_name → client → 'unknown'. Never
            # leak transport plumbing IDs into analytics.
            # r44 (2026-05-25): store session_id from Mcp-Session-Id
            # header (captured into _r_session above). Column added via
            # mcp_growth._SCHEMA_DDL ALTER ... ADD COLUMN IF NOT EXISTS.
            _c_lt.execute(
                """INSERT INTO mcp_tool_calls
                       (tool_name, platform, client_name, params, success,
                        response_time_ms, ip_address, user_agent, session_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(tool)[:200],
                    (_platform_clean or 'mcp-worker')[:80],
                    (_r_client or 'unknown')[:200],
                    (_params_str or '{}')[:4000],
                    bool((body.get('status') in (None, 'ok', 'success', 200, True)) or body.get('success', True)),
                    int((body.get('duration_ms') or body.get('response_time_ms') or 0) or 0),
                    # item-3 (real caller IP): prefer the IP forwarded in the
                    # payload — server.mjs now captures the inbound client's
                    # X-Forwarded-For first hop and sends it as body.ip_address.
                    # The request header / remote_addr here is the Node MCP
                    # server's own egress IP (same reason user_agent read 'node'
                    # for every row), so it's only a last-resort fallback.
                    (body.get('ip_address') or request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:64],
                    # r78: prefer the CLIENT UA forwarded in the payload —
                    # the request header here is the Node server's own
                    # fetch UA, which is why every row since 5/18 read
                    # user_agent='node'.
                    ((body.get('user_agent') or request.headers.get('User-Agent') or ''))[:300],
                    (str(_r_session)[:200] if _r_session else None),
                )
            )
            _db_lt.commit()
    except Exception as _e_lt:
        try: import logging as _log9j; _log9j.getLogger(__name__).warning('phase9j_dual mcp_tool_calls insert: %s', _e_lt)
        except Exception: pass
    finally:
        if _db_lt is not None:
            try: _db_lt.close()   # ALWAYS return the connection to the pool
            except Exception: pass

    # r-unlock-signal (2026-07-03): unlock_more_data handing checkout links to
    # a KEYED agent is the agent path's upgrade-intent moment, but it left no
    # mcp_upgrade_signals row — upgrade events were email-keyed only, and 11 of
    # 12 claim redemptions have NO email (agents auto-redeem), so agent-path
    # trial→upgrade intent was structurally invisible. Record it through the
    # canonical writer (hourly per-caller dedup inside; synthetic traffic is
    # filtered at the read layer, but skip our own self-heal loop outright).
    if (str(tool) == 'unlock_more_data' and not _is_synthetic_selfheal
            and (body.get('api_key') or '').strip()):
        try:
            from mcp_signal_canonical import record_signal
            record_signal(
                signal_type='checkout_link_issued',
                tool_requested='unlock_more_data',
                tier_current=(body.get('tier') or 'free'),
                session_id=(str(_r_session)[:200] if _r_session else None),
                mcp_client=(_r_client or None),
                user_agent=((body.get('user_agent')
                             or request.headers.get('User-Agent') or '')[:300]
                            or None),
                ip_address=((body.get('ip_address')
                             or request.headers.get('X-Forwarded-For')
                             or request.remote_addr or '')[:64] or None),
                api_key=(body.get('api_key') or '').strip(),
            )
        except Exception:
            pass  # telemetry side-channel — never block the track callback

    # r-claim-session-bind (2026-07-21, flag DCHUB_CLAIM_SESSION_BIND, default on):
    # ACTIVATION FIX for the claim_free_key retention leak. An agent claims a key
    # mid-session but its subsequent calls don't carry it — server.mjs binds only
    # on paid-GATED tools AND only when the session is already in that replica's
    # in-memory sessionMeta, so free-tool calls (and any call on a cold/other
    # replica) stay anon. The claimed key then looks unused (last_used_at never
    # advances) and no reuse history builds — the confirmed bulk of the "4%
    # retention" (live: only ~18% of post-claim sessions carried the key). Resolve
    # it server-side HERE: when a tracked call has NO api_key but its session owns a
    # recently-claimed key, attribute the call to that key AND advance
    # last_used_at. Cross-replica-safe (one backend DB), no mcp-server change, no
    # added agent latency (track is fire-and-forget). Honest: same session_id = the
    # same agent that owns the key. Telemetry layer only — does NOT alter live
    # gating/tier (that stays the mcp-server's job).
    _eff_api_key = (body.get("api_key") or "").strip() or None
    if (not _eff_api_key and _r_session
            and os.environ.get("DCHUB_CLAIM_SESSION_BIND", "1") != "0"):
        _bk = _resolve_session_claimed_key(_tc_conn, str(_r_session)[:200])
        if _bk:
            _eff_api_key = _bk
            try:
                with _tc_conn.cursor() as _uc:
                    _uc.execute(
                        "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                        (_bk,),
                    )
            except Exception:
                pass  # activation advance is best-effort — never block tracking

    try:
        if _tc_conn is None:
            _tc_conn = _open_track_conn()
        with _tc_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_call_log
                     (timestamp, tool, params, platform, api_key, tier,
                      session_id, status, duration_ms, referrer, user_agent, event_type)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    ts_dt, tool, params,
                    (_r_platform or body.get("platform")),
                    _eff_api_key,
                    body.get("tier"),
                    body.get("session_id"),
                    body.get("status"),
                    (body.get('duration_ms') or (body.get('response_time_ms') or body.get('duration_ms'))),
                    # r46 (2026-05-25): attribution for v_paywall_attribution view
                    body.get("referer") or body.get("referrer"),
                    (body.get("user_agent") or "")[:500] or None,
                    # r47 (2026-05-25): derive event_type from status so views
                    # don't need backfills going forward.
                    {"blocked_paid_only": "paywall_block",
                     "trial_used":        "trial_preview",
                     "ok":                "tool_call",
                     "error":             "tool_error"}.get(body.get("status")),
                ),
            )
        # Monthly-quota counting rail (see monthly_quota.py): same autocommit
        # connection, separate guard — a rollup miss must never fail the
        # track callback or the mcp_call_log row above.
        #
        # ★ 2026-08-06 (phase 2, design decision (b)): only status='ok' burns
        # quota. Phase 1 counted EVERY tracked call, so a paywalled block, a
        # tool error or a trial preview ate the caller's month — and the
        # quota is about to start blocking. This matches
        # /api/v1/mcp/usage-today, which has always filtered status='ok'.
        # Consequence for the log-only review: the July baseline was measured
        # with failures included, so post-change usage is <= baseline for
        # every key and the blast radius can only shrink.
        if _eff_api_key and not _is_synthetic_selfheal:
            try:
                from monthly_quota import counts_toward_quota, record_monthly_call
                if counts_toward_quota(body.get("status")):
                    with _tc_conn.cursor() as _mq_cur:
                        record_monthly_call(_mq_cur, _eff_api_key, ts_dt)
            except Exception:
                try:
                    from routes._swallowed_writes import note_swallowed_write
                    note_swallowed_write("mcp_monthly_usage",
                                         where="flask_mcp_endpoints.track_tool_call")
                except Exception:
                    pass
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    finally:
        if _tc_conn is not None:
            try:
                _tc_conn.close()
            except Exception:
                pass

    return jsonify({"ok": True}), 200


# r-continuation (2026-09-03): the human-line variant, recorded so the claim
# behind it can be settled. Lives in its own import-free module because
# tests/ deliberately never imports Flask or the DB — see its docstring.
from relay_specificity import tag_relay_specificity


# ── POST /api/v1/mcp/signal-paywall — record a paywall/preview signal ────
#
# 2026-06-06 (MCP-C): restore tool_requested write from the MCP server's
# trial_preview + blocked_paid_only branches. Prior to this fix, only
# Python paths (mcp_upgrade_gate.gate_tool_call + pair_code redeem) wrote
# mcp_upgrade_signals.tool_requested — the Node MCP server (server.mjs)
# in dchub-mcp-server fired NO signal at all when it returned a
# trial_preview or blocked_paid_only response. Result: the funnel
# reported 0 upgrade signals tagged with tool_requested for the busiest
# paid tool (get_grid_intelligence: 4,540 paywall hits, 0 tagged
# signals). Per-tool funnel optimization was structurally blind.
#
# This endpoint is the fire-and-forget telemetry hook the MCP server
# calls inside its paywall branch. It delegates to fire_upgrade_signal
# from mcp_upgrade_gate.py, which already handles synthetic-traffic
# exclusion, api_key → user_email resolution, and the canonical
# mcp_upgrade_signals INSERT (tool_requested column populated).
@mcp_bp.post("/api/v1/mcp/signal-paywall")
@_require_internal
def mcp_signal_paywall():
    body = request.get_json(silent=True) or {}
    tool = (body.get('tool') or body.get('tool_name') or '').strip()
    if not tool:
        return jsonify({"ok": False, "error": "missing tool"}), 200

    signal_type = (body.get('signal_type') or 'trial_preview').strip()
    # Valid signal_type values used elsewhere in the codebase:
    #   trial_preview, paid_tool_blocked, daily_limit_hit, redeem_url_viewed
    # Accept any so the MCP server can label new branches without a
    # Flask redeploy, but fall back to trial_preview if the caller
    # passes something obviously wrong.
    if signal_type not in (
        'trial_preview', 'paid_tool_blocked', 'daily_limit_hit',
        'redeem_url_viewed', 'anon_preview', 'blocked_paid_only',
    ):
        signal_type = 'trial_preview'

    # r44 (2026-05-25): prefer the modern Mcp-Session-Id HTTP header
    # over the body field for the same reasons as /api/v1/mcp/track.
    session_id = (
        request.headers.get("Mcp-Session-Id")
        or request.headers.get("mcp-session-id")
        or request.headers.get("X-Mcp-Session-Id")
        or body.get("session_id")
    )

    # mcp_client (platform) for synthetic-traffic exclusion. fire_upgrade_signal
    # short-circuits when mcp_client matches _SYNTHETIC_CLIENT_PREFIXES so
    # our own probes don't pollute the funnel.
    mcp_client = (body.get('mcp_client') or body.get('platform') or 'mcp').strip()
    user_agent = body.get('user_agent') or request.headers.get('User-Agent')
    api_key    = body.get('api_key')  # optional — lifted from headers if missing
    user_email = body.get('user_email')
    message_shown = (body.get('message_shown') or '')[:2000] or None
    message_shown = tag_relay_specificity(message_shown, body.get('relay_specificity'))
    tier_current = body.get('tier_current') or 'free'
    tier_required = body.get('tier_required') or 'paid'

    # Brain L6 #1264 — paid-intent ledger. Capture FIRST + INDEPENDENTLY: this used
    # to sit after fire_upgrade_signal's except-return, so any telemetry failure
    # silently skipped the lead capture (the 0-rows bug). Now it always runs. This
    # handler is the LIVE hook the Node MCP server calls on every grid/fiber paywall
    # (signalPaywall) — capturing the real ANON traffic the Flask _gate_mcp_result
    # path never sees. Fire-and-forget; grid/fiber-filtered inside; never blocks.
    try:
        from routes.paid_intent_ledger import record_from_signal
        record_from_signal(
            tool=tool, signal_type=signal_type, api_key=api_key, email=user_email,
            ip=(request.headers.get("CF-Connecting-IP")
                or (request.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip()
                or request.remote_addr or ""),
            user_agent=user_agent, session_id=session_id,
            args=(body.get("args") or body.get("arguments") or {}))
    except Exception as _pie:
        print(f"[paid_intent] hook error: {_pie}", flush=True)

    try:
        from mcp_upgrade_gate import fire_upgrade_signal
        fire_upgrade_signal(
            signal_type=signal_type,
            tool_requested=tool,
            tier_current=tier_current,
            tier_required=tier_required,
            message_shown=message_shown,
            mcp_client=mcp_client,
            user_agent=user_agent,
            session_id=session_id,
            user_email=user_email,
            api_key=api_key,
        )
    except Exception as e:
        # Telemetry is fire-and-forget — never 500 the MCP request path.
        return jsonify({"ok": False, "error": str(e)}), 200

    return jsonify({"ok": True}), 200


# ── GET /api/v1/mcp/continuation-compliance — DID THE AGENT ACT? ──────────
#
# The eighth row of the funnel instrumentation table, the one that was called
# unmeasurable: "did the agent surface the continuation at all?"
#
# It is measurable, because the continuation instruction IS a tool call. Every
# gated response names a `next_tool` — unlock_more_data / claim_free_key /
# bind_email — and an agent that preserved and acted on the instruction calls
# it, in the same session, after the gate. mcp_upgrade_signals writes
# session_id; mcp_calls_identity exposes it. No new telemetry, no human needed.
#
# Why that matters: "did a human open the link" ran n=1 over 5,704 signals and
# is therefore useless as a signal about anything. "Did the agent do the thing
# we asked" happens thousands of times a week and is fully observed. It also
# separates the two explanations that a dead link cannot: the human ignored it,
# versus the human never saw it.
#
# ★ And it splits by arm. message_shown carries :quantified or :generic, so the
# same query answers the copy experiment — does naming what was withheld get
# acted on more than not naming it?
#
# READ-ONLY. One SELECT, internal-key gated, no writes anywhere.
@mcp_bp.get("/api/v1/mcp/continuation-compliance")
@_require_internal
def mcp_continuation_compliance():
    from continuation_compliance import CONTINUATION_TOOLS, summarize_compliance
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except ValueError:
        days = 7

    # DISTINCT ON pins each session to its FIRST gate, so a session that saw
    # two gates is counted once and attributed to the arm it actually met
    # first. Counting it in both arms would let one session move both rates.
    # continued_sessions is the SAME join without the tool filter: did this
    # session make ANY real external call after the gate? A session that made
    # none had no turn in which to comply, so counting it as non-compliance
    # would report a gateway's session model as an agent's refusal.
    sql = """
        WITH gated AS (
            SELECT DISTINCT ON (session_id)
                   session_id,
                   created_at    AS first_gate,
                   message_shown,
                   COALESCE(NULLIF(TRIM(LOWER(mcp_client)), ''), 'unattributed')
                       AS client
              FROM mcp_upgrade_signals
             WHERE created_at >= now() - make_interval(days => %s)
               AND session_id IS NOT NULL AND session_id <> ''
               AND message_shown LIKE 'trial_preview%%'
             ORDER BY session_id, created_at
        )
        SELECT g.message_shown,
               g.client,
               COUNT(*)                          AS gated_sessions,
               COUNT(*) FILTER (WHERE a.any_call) AS continued_sessions,
               COUNT(*) FILTER (WHERE a.acted)    AS acted_sessions
          FROM gated g
          LEFT JOIN LATERAL (
              -- ONE pass over the post-gate calls per session, answering both
              -- questions. Two EXISTS subqueries would walk mcp_calls_identity
              -- twice for the same rows, and this route already runs a GROUP BY
              -- per request — the pool-saturation trap this file warns about.
              SELECT COUNT(*) > 0                                     AS any_call,
                     COUNT(*) FILTER (WHERE c.tool_name = ANY(%s)) > 0 AS acted
                FROM mcp_calls_identity c
               WHERE c.session_id  = g.session_id
                 AND c.created_at  > g.first_gate
                 AND c.is_real_external
          ) a ON TRUE
         GROUP BY g.message_shown, g.client
    """
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (days, list(CONTINUATION_TOOLS)))
            rows = cur.fetchall() or []
    except Exception as e:
        # A query that cannot run reports that it could not run. It does NOT
        # report zeros — an unreachable table and a silent agent produce the
        # same 0 and mean opposite things.
        return jsonify({"ok": False, "window_days": days,
                        "state": "UNMEASURED",
                        "error": f"{type(e).__name__}: {e}"[:300]}), 200

    out = summarize_compliance(rows)
    out.update({
        "ok": True,
        "window_days": days,
        "measures": "share of gated SESSIONS in which the agent afterwards "
                    "called one of the tools the gated response told it to call",
        "session_attribution": "first gate per session (DISTINCT ON), so a "
                               "session that met two gates counts once, in the "
                               "arm it met first",
        "opportunity": "continued_sessions counts gated sessions that made ANY "
                       "real external call afterwards. A bucket where nothing "
                       "continued is UNMEASURED, not 0%: a client whose "
                       "sessions are one call long never had a turn to comply.",
        "concentration_unit": "gated SESSIONS on mcp_upgrade_signals — NOT the "
                              "tool-call concentration /api/v1/ai/reach "
                              "publishes on mcp_calls_identity. Different "
                              "basis, different number; never divide across "
                              "the two.",
        "client_identity": "mcp_client — the SELF-DECLARED client string, not "
                           "the IP-derived agent_id /api/v1/ai/reach counts "
                           "agents by. Session durability is a property of the "
                           "client SOFTWARE, so the software is what this "
                           "splits on; the two identities do not line up and "
                           "their counts are not interchangeable.",
        "read_this_before_the_relay_funnel": "relay opens ran 1 in 5,704 and "
                                             "cannot distinguish 'the human "
                                             "ignored it' from 'the human never "
                                             "saw it'. This can.",
    })
    return jsonify(out), 200


# ── GET /api/v1/mcp/stats — for our own admin dashboard ───────────────────

@mcp_bp.get("/api/v1/mcp/stats")
@_require_internal
def mcp_stats():
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except ValueError:
        days = 7

    out = {"window_days": days}

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('day', timestamp)::date AS day, platform, COUNT(*) AS n
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY day, platform ORDER BY day DESC, n DESC""",
            (days,),
        )
        out["by_day_platform"] = [
            {"day": str(r[0]), "platform": r[1], "n": r[2]} for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT tool,
                      COUNT(*)::int AS n,
                      AVG(duration_ms)::int AS avg_ms,
                      COUNT(*) FILTER (WHERE status='error')::int AS errors,
                      COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS upgrade_blocks,
                      COUNT(DISTINCT api_key)::int AS distinct_devs
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY tool ORDER BY n DESC""",
            (days,),
        )
        out["by_tool"] = [
            {"tool": r[0], "n": r[1], "avg_ms": r[2],
             "errors": r[3], "upgrade_blocks": r[4], "distinct_devs": r[5]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE api_key IS NOT NULL)::int AS keyed_calls,
                 COUNT(DISTINCT api_key) AS keyed_devs,
                 COUNT(DISTINCT session_id) AS sessions,
                 COUNT(*)::int AS tool_calls,
                 COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS paid_block_events
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)""",
            (days,),
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0)
        out["funnel"] = {
            "keyed_calls":       r[0] or 0,
            "keyed_devs":        r[1] or 0,
            "sessions":          r[2] or 0,
            "tool_calls":        r[3] or 0,
            "paid_block_events": r[4] or 0,
        }

        cur.execute(
            "SELECT tier, COUNT(*)::int FROM mcp_dev_keys WHERE status='active' GROUP BY tier ORDER BY tier"
        )
        out["keys_by_tier"] = [{"tier": r[0], "n": r[1]} for r in cur.fetchall()]

    return jsonify(out), 200


# ── POST /api/v1/dev-signup — Self-serve free dev key (PUBLIC) ────────────

@mcp_bp.post("/api/v1/dev-signup")
def dev_signup():
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "valid email required"}), 400

    api_key      = f"dch_live_{secrets.token_hex(16)}"
    developer_id = f"dev_{secrets.token_hex(8)}"

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key FROM mcp_dev_keys WHERE email=%s AND status='active' LIMIT 1",
                (email,),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({
                    "api_key":     existing[0],
                    "tier":        "free",
                    "email":       email,
                    "is_new":      False,
                    "header":      "X-API-Key",
                    "docs":        "https://dchub.cloud/ai",
                    # Phase FF+7 (2026-05-19): /upgrade entry-point (see /keys/claim above)
                    "upgrade_url": f"https://dchub.cloud/upgrade?key={existing[0]}",
                }), 200
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'free', 'active', %s::jsonb)""",
                (api_key, developer_id, email, '{"source":"dev-signup-form"}'),
            )
    except Exception as e:
        return jsonify({"error": "key issuance failed", "detail": str(e)}), 500

    return jsonify({
        "api_key":     api_key,
        "tier":        "free",
        "email":       email,
        "is_new":      True,
        "header":      "X-API-Key",
        "docs":        "https://dchub.cloud/ai",
        # Phase FF+7 (2026-05-19): /upgrade entry-point with attribution
        "upgrade_url": f"https://dchub.cloud/upgrade?key={api_key}",
    }), 200


# ── POST /api/v1/admin/billing/reconcile-keys ─ recover stuck paid keys ───
# Root cause (billing tier-table gap): the Stripe webhook
# (handle_checkout_completed, main.py) upgrades mcp_dev_keys.tier ONLY by an
# email match on an ALREADY-active key. mcp_dev_keys has no user_id FK — email
# is the only link — so pay-then-claim races / email mismatches leave real
# payers stuck at tier='free' despite paying. This endpoint finds real payers
# (paid plan + a Stripe customer + active sub) whose email-matched active key
# is below the paid tier and (with apply=1) upgrades it. GET or apply=0 = safe
# dry-run report. Admin-gated. Idempotent — safe to run on a schedule.
@mcp_bp.get("/api/v1/admin/billing/reconcile-keys")
@mcp_bp.post("/api/v1/admin/billing/reconcile-keys")
def admin_reconcile_keys():
    try:
        from routes.funnel_health import _admin_ok
        if not _admin_ok(request):
            return jsonify(ok=False, error="unauthorized"), 401
    except Exception:
        return jsonify(ok=False, error="auth_unavailable"), 503
    apply = (request.args.get("apply") in ("1", "true", "yes"))
    _full = (request.args.get("full") in ("1", "true", "yes"))  # admin-only: un-redact for outreach
    PAID_PLANS = ("developer", "pro", "founding", "enterprise")  # starter is web-only, not MCP-paid
    def _redact(e):
        if _full:
            return e
        try:
            u, d = e.split("@", 1)
            return (u[:2] + "***@" + d)
        except Exception:
            return "***"
    out = {"ok": True, "apply": apply, "keys_upgraded": 0,
           "real_payers": 0,
           "buckets": {"already_paid_key": 0, "key_upgraded": 0,
                       "unlinked_no_matching_key": 0},
           "samples": {"upgraded": [], "unlinked": []}}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, LOWER(email) AS email, LOWER(COALESCE(plan,'')) AS plan
                  FROM users
                 WHERE LOWER(COALESCE(plan,'')) = ANY(%s)
                   AND COALESCE(stripe_customer_id,'') <> ''
                   AND LOWER(COALESCE(subscription_status,'active'))
                       = ANY(ARRAY['active','trialing','past_due'])
                   AND COALESCE(email,'') <> ''
            """, (list(PAID_PLANS),))
            payers = cur.fetchall()
            out["real_payers"] = len(payers)
            for uid, email, plan in payers:
                want = "enterprise" if plan == "enterprise" else "paid"
                cur.execute(
                    "SELECT tier FROM mcp_dev_keys "
                    " WHERE LOWER(COALESCE(email,'')) = %s AND status='active'",
                    (email,))
                tiers = [(r[0] or "").lower() for r in cur.fetchall()]
                if not tiers:
                    out["buckets"]["unlinked_no_matching_key"] += 1
                    if len(out["samples"]["unlinked"]) < 25:
                        out["samples"]["unlinked"].append({"email": _redact(email), "plan": plan})
                    continue
                if any(t in ("paid", "enterprise") for t in tiers):
                    out["buckets"]["already_paid_key"] += 1
                    continue
                out["buckets"]["key_upgraded"] += 1
                if len(out["samples"]["upgraded"]) < 50:
                    out["samples"]["upgraded"].append(
                        {"email": _redact(email), "plan": plan, "want_tier": want})
                if apply:
                    cur.execute(
                        "UPDATE mcp_dev_keys SET tier=%s "
                        " WHERE LOWER(COALESCE(email,''))=%s AND status='active' "
                        "   AND COALESCE(tier,'') NOT IN ('paid','enterprise')",
                        (want, email))
                    out["keys_upgraded"] += (cur.rowcount or 0)
            if apply:
                conn.commit()
        return jsonify(out)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# ── POST /api/v1/admin/track/reconcile-session-keys ──────────────────────────
# r-track-reconcile (2026-07-21): the RETENTION actuator, auto-fired daily — the
# ONE loop we close on the constraint the flywheel keeps naming. The request-time
# /track resolver (DCHUB_CLAIM_SESSION_BIND) attributes a post-claim call to its
# session's claimed key at WRITE time, but only for NEW calls that resolve inside
# the 24h window with a cache hit. Calls that already wrote NULL (cold/other
# replica, cache miss, flag-off window, or pre-2026-07-21) stay unattributed →
# mcp_call_log.api_key NULL + mcp_dev_keys.last_used_at stale → the "only ~18%
# carried the key" UNDER-measurement. This batch sweeps them: attribute NULL-key
# call rows to the session's claimed key (the SAME join the resolver uses) +
# advance last_used_at.
#   SAFE — pure in-DB UPDATEs, NO outbound HTTP (not a self-request → cannot
#     reproduce the master-shell pool-saturation incident); telemetry layer only
#     (never touches tier/plan/gating, same boundary as the request-time resolver).
#   IDEMPOTENT — only fills NULLs (filled rows drop out of the filter); last_used_at
#     uses GREATEST so re-runs never regress or double-advance.
#   REVERSIBLE — flag DCHUB_TRACK_RECONCILE (off = no-op) + `apply` param
#     (default 0 = safe dry-run report). Admin-gated. Bounded lookback (≤30d).
@mcp_bp.get("/api/v1/admin/track/reconcile-session-keys")
@mcp_bp.post("/api/v1/admin/track/reconcile-session-keys")
def admin_reconcile_session_keys():
    try:
        from routes.funnel_health import _admin_ok
        if not _admin_ok(request):
            return jsonify(ok=False, error="unauthorized"), 401
    except Exception:
        return jsonify(ok=False, error="auth_unavailable"), 503
    if (os.environ.get("DCHUB_TRACK_RECONCILE", "1") or "").strip() == "0":
        return jsonify(ok=True, disabled=True, note="DCHUB_TRACK_RECONCILE=0")
    apply = (request.args.get("apply") in ("1", "true", "yes"))
    try:
        days = max(1, min(30, int(request.args.get("days", "14"))))
    except Exception:
        days = 14
    out = {"ok": True, "apply": apply, "lookback_days": days,
           "candidates": 0, "attributed": 0, "keys_touched": 0, "sample": []}
    # a NULL-key call row whose session owns a recent active claim key minted
    # at/before the call — the same identity the request-time resolver uses.
    _match = (" k.metadata->>'session_id' = %(sidcol)s "
              " AND k.status='active' AND k.created_at >= now() - interval '30 days' "
              " AND (k.api_key LIKE 'dch_live_%%' OR k.api_key LIKE 'dch_trial_%%') ")
    # The temporal guard. Was `k.created_at <= <call>.timestamp`, which bounded
    # the back-fill only while metadata.session_id was write-once at mint.
    # _restamp_claim_session (2026-07-25) re-points a REUSED key at the session
    # claiming it now, decoupling created_at from bind time by up to the 30d
    # reuse window — so created_at alone would let this sweep attribute a
    # session's PRE-claim anonymous calls to the key and inflate the carry /
    # activation metrics. Prefer the recorded bind time; fall back to
    # created_at for keys minted before that field existed.
    _bound_at = ("COALESCE(NULLIF(k.metadata->>'session_bound_at','')::timestamptz,"
                 " k.created_at)")
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM mcp_call_log l "
                " WHERE l.api_key IS NULL AND l.session_id IS NOT NULL AND l.session_id <> '' "
                "   AND l.timestamp >= now() - make_interval(days => %(d)s) "
                "   AND EXISTS (SELECT 1 FROM mcp_dev_keys k WHERE "
                + _match.replace("%(sidcol)s", "l.session_id")
                + " AND " + _bound_at + " <= l.timestamp)", {"d": days})
            out["candidates"] = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                "SELECT DISTINCT ON (l.id) l.session_id, l.tool, k.api_key "
                "  FROM mcp_call_log l JOIN mcp_dev_keys k ON "
                + _match.replace("%(sidcol)s", "l.session_id")
                + " AND " + _bound_at + " <= l.timestamp "
                " WHERE l.api_key IS NULL AND l.session_id IS NOT NULL AND l.session_id <> '' "
                "   AND l.timestamp >= now() - make_interval(days => %(d)s) "
                " ORDER BY l.id, k.created_at DESC LIMIT 8", {"d": days})
            out["sample"] = [{"session": (s or "")[:12], "tool": t, "key": "…" + (kk or "")[-6:]}
                             for s, t, kk in cur.fetchall()]
            if apply and out["candidates"] > 0:
                cur.execute(
                    "UPDATE mcp_call_log l SET api_key = sub.api_key "
                    "  FROM (SELECT DISTINCT ON (l2.id) l2.id, k.api_key "
                    "          FROM mcp_call_log l2 JOIN mcp_dev_keys k ON "
                    + _match.replace("%(sidcol)s", "l2.session_id")
                    + " AND " + _bound_at + " <= l2.timestamp "
                    "        WHERE l2.api_key IS NULL AND l2.session_id IS NOT NULL AND l2.session_id <> '' "
                    "          AND l2.timestamp >= now() - make_interval(days => %(d)s) "
                    "        ORDER BY l2.id, k.created_at DESC) sub "
                    " WHERE l.id = sub.id", {"d": days})
                out["attributed"] = int(cur.rowcount or 0)
                cur.execute(
                    "UPDATE mcp_dev_keys k "
                    "   SET last_used_at = GREATEST(COALESCE(k.last_used_at, to_timestamp(0)), sub.mx) "
                    "  FROM (SELECT api_key, MAX(timestamp) mx FROM mcp_call_log "
                    "         WHERE api_key IS NOT NULL AND timestamp >= now() - make_interval(days => %(d)s) "
                    "         GROUP BY api_key) sub "
                    " WHERE k.api_key = sub.api_key "
                    "   AND (k.api_key LIKE 'dch_live_%%' OR k.api_key LIKE 'dch_trial_%%') "
                    "   AND (k.last_used_at IS NULL OR k.last_used_at < sub.mx)", {"d": days})
                out["keys_touched"] = int(cur.rowcount or 0)
                conn.commit()
        return jsonify(out)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# PRESS_HEADLINE_CANON_FIELD / _CANON_WOW_FIELD were deleted 2026-08-05. They
# named the ROLLING pair the sentence must no longer bind, and a constant that
# names a field is an invitation to bind it. The fields themselves stay in the
# funnel payload as data; only the quotable string moved off them.

PRESS_HEADLINE_BASIS = (
    "weekly figure and WoW = the last COMPLETE ISO week of "
    "GET /api/v1/reports/weekly-series (weeks[-1].calls and wow.calls_pct, "
    "wow.baseline_is_fixed=true) — the CANONICAL identity population "
    "(mcp_calls_identity, agent_id, is_public_ip AND is_real_external; see "
    "real_external_agents_basis for the full definition) on a FIXED window. "
    "Deliberately NOT the rolling real_external_calls_7d / "
    "real_external_calls_wow_pct pair, which is correct as data but "
    "recomputes per request and so cannot be quoted verbatim; NOT "
    "tool_calls_7d_complete_real (complete-days population); NOT "
    "real_external_signals_7d (mcp_upgrade_signals, a signal count). Several "
    "weekly populations and windows live in this payload; this sentence "
    "quotes exactly one pair and names it here. Both numbers are stable "
    "between reads and stay stable until the next ISO week closes."
)

# Fixed-window source for the quotable sentence. Imported lazily inside the
# fetcher: this module is imported at app start by things that must not pull
# the whole reports package, and a hard import failure here must degrade the
# headline, never the funnel.
PRESS_HEADLINE_SERIES_WEEKS = 10
# weeks[] only changes when an ISO week closes, so this TTL costs the sentence
# nothing in freshness. It exists so the public funnel does not run a 10-week
# GROUP BY per request — the pool-saturation trap, not a correctness one.
PRESS_HEADLINE_SERIES_TTL_S = 600
_press_series_cache = {"data": None, "ts": 0.0}


def _press_series():
    """The weekly-series payload, memoised. None when it cannot be read."""
    now = time.time()
    if (_press_series_cache["data"] is not None
            and now - _press_series_cache["ts"] < PRESS_HEADLINE_SERIES_TTL_S):
        return _press_series_cache["data"]
    try:
        from routes.weekly_series import _run as _series_run
        data = _series_run(PRESS_HEADLINE_SERIES_WEEKS)
    except Exception:
        return None
    if not data or data.get("degraded"):
        return None
    _press_series_cache["data"] = data
    _press_series_cache["ts"] = now
    return data


def _fixed_window_claim(series):
    """(calls, wow_pct, week_start) for the quotable sentence — PURE.

    Selection rules, each one a way the sentence could otherwise start moving
    or start quoting a delta it cannot name:

      · only COMPLETE, MEASURED weeks — a partial week re-reads larger every
        hour, which is the whole defect, and `status != measured` means the
        week was not observed (null is not zero)
      · the delta is quoted only when the series vouches for a FIXED baseline
        (wow.baseline_is_fixed) AND wow.current_week_start is the very week
        being quoted. A delta computed against some other pair is not the
        delta for this level
      · no delta ⇒ (calls, None, week) — publish the level and withhold the
        percentage, never fall back to a rolling one

    Returns (None, None, None) when no complete week is available.
    """
    if not series or series.get("degraded"):
        return (None, None, None)
    complete = [w for w in (series.get("weeks") or [])
                if isinstance(w, dict) and not w.get("partial")
                and w.get("status") == "measured" and w.get("calls") is not None]
    if not complete:
        return (None, None, None)
    week = complete[-1]
    try:
        calls, start = int(week["calls"]), week.get("week_start")
    except (TypeError, ValueError):
        return (None, None, None)
    wow = series.get("wow") or {}
    if not isinstance(wow, dict):
        return (calls, None, start)
    pct = wow.get("calls_pct")
    if not (wow.get("baseline_is_fixed") and pct is not None
            and wow.get("current_week_start") == start):
        return (calls, None, start)
    # ★ A FIXED baseline is not automatically a COMPARABLE one (2026-08-19).
    # weekly_series now publishes comparability.crosses_definition_change when
    # a week in the delta counts a different population from the others. The
    # first such marker is dchub-mcp-server#202 (2026-08-18 06:31Z), which
    # removed DC Hub's own CI from is_real_external — 80.4% of real calls and
    # 72.1% of real agents in the 7d before it. Quoting that as a WoW would
    # put "calls fell ~80%" into a press-ready sentence designed to be
    # repeated verbatim, describing a measurement correction as a collapse in
    # demand. The LEVEL is still true and still published; only the delta is
    # withheld, on exactly the same terms as every other refusal here.
    #
    # ★ 2026-08-20 — CROSSES WAS NOT ENOUGH, AND THE GAP SHIPPED A PRESS LINE.
    # crosses_definition_change only fires when the change lands INSIDE a week
    # of the delta. #202 landed 2026-08-18, AFTER both weeks of the then-live
    # delta (2026-08-03 -> 2026-08-10), so it fired on neither and this
    # function published "-11.3% WoW" on 2,100 calls — a week in which ~80% of
    # those calls were our own GitHub Actions. A press-ready sentence quoting
    # our CI cadence as external demand is the exact failure the block above
    # was written to prevent; it just could not see this shape of it.
    # superseded_by_correction closes it. Both booleans are checked by name so
    # the payload can say WHICH hazard withheld the delta.
    comp = wow.get("comparability")
    if isinstance(comp, dict) and (comp.get("crosses_definition_change")
                                   or comp.get("superseded_by_correction")):
        return (calls, None, start)
    try:
        # A malformed delta costs the DELTA, never the level and never the
        # whole sentence — the outer handler used to swallow anything raised
        # here and publish no press_headline_metric key at all.
        return (calls, float(pct), start)
    except (TypeError, ValueError):
        return (calls, None, start)


def _week_spans(week_start_dates):
    """[date, ...] -> half-open [Mon 00:00Z, next Mon 00:00Z) UTC spans."""
    from datetime import time as _t, timedelta as _td
    return [(datetime.combine(d, _t.min, tzinfo=timezone.utc),
             datetime.combine(d + _td(weeks=1), _t.min, tzinfo=timezone.utc))
            for d in week_start_dates]


def _rolling_spans(days: int, count: int):
    """The `count` consecutive trailing windows of `days`, most recent first.

    _rolling_spans(7, 2) == [(now-7d, now), (now-14d, now-7d)] — exactly the
    pair real_external_*_wow_pct divides.
    """
    from datetime import timedelta as _td
    now = datetime.now(timezone.utc)
    return [(now - _td(days=days * (i + 1)), now - _td(days=days * i))
            for i in range(count)]


def _mark_wow_comparability(out: dict, spans, pct_keys, prefix: str) -> None:
    """Attach comparability to FLAT `*_wow_pct` keys, and null them if unsafe.

    ★ 2026-08-20 — WHY NULLING, NOT JUST FLAGGING. weekly-series can publish a
    tainted pct beside a comparability block because its consumers are readers
    that branch. These keys are read by the funnel DASHBOARD, which renders the
    scalar directly — that is how "+89.5% WoW on COMPLETE weeks (38 -> 72)"
    reached the screen labelled "the trend number" while both weeks sat on the
    superseded side of #202, next to a rolling "-28.8% crash" computed across
    the same correction. A flag the renderer does not read changes nothing.

    None is already this key's contract for "no delta available" (a zero
    baseline yields it), so consumers must already handle it. The LEVEL keys
    are untouched and still published — only the delta is withheld, on the same
    terms as the press headline.

    Fail-soft: comparability is metadata about honesty, and losing the marker
    must never cost the payload. Absent keys read exactly as before this existed.
    """
    try:
        from routes.weekly_series import comparability_for_spans
        comp = comparability_for_spans(spans)
    except Exception:
        return
    out[f"{prefix}_comparability"] = comp
    if comp.get("quotable_as_trend"):
        return
    withheld = [k for k in pct_keys if out.get(k) is not None]
    for k in pct_keys:
        if out.get(k) is not None:
            out[f"{k}_withheld"] = out[k]   # keep the arithmetic, named as such
            out[k] = None
    if withheld:
        out[f"{prefix}_withheld_reason"] = comp.get("means")


def _build_press_headline(out: dict, series=None) -> None:
    """Build press_headline_metric + press_headline_metric_basis on `out`.

    `series` is the weekly-series payload; None means fetch it. Injectable so
    the fence tests can drive every branch on static payloads without a DB —
    the shipped bytes, not a reimplementation of them.

    Press-ready single-sentence signal.
    brain-l15 #1439/#1447: quote the EXTERNAL figure (internal/self-heal
    excluded) and name the ACTUAL top external platforms, so the press line
    is no longer "led by Claude and ChatGPT" over ~68% self-heal traffic.
    brain-l15 #1656/#1661 (2026-07-18): LEAD with the current weekly external
    figure + WoW delta — a number that visibly moves week-over-week — and
    demote the monotonically-growing lifetime counter to a secondary clause.
    A cumulative headline structurally cannot show that the current run-rate
    collapsed after a one-time spike; this can.

    ★ REPOINTED 2026-08-05 (population collision). The headline bound
    tool_calls_7d_complete_real + tool_calls_wow_pct — the complete-days
    population — while the canonical rolling identity figure is
    real_external_calls_7d. Measured 2026-08-05, that meant the sentence
    quoted 7,159 (+97.1%) while canon read 6,868 (+87.4%): of the three
    weekly populations in this payload the headline had bound the LARGEST,
    which is exactly the choice a published number must not make for itself.
    This string is designed to be quoted verbatim, so it is the last place a
    flattering-population default belongs.

    ★ Extracted to a module-level helper in the same change because the
    canonical pair is computed ~500 lines below where the headline used to
    be assembled. Repointing in place would have read None off `out` and
    silently degraded the weekly claim to the lifetime-only sentence every
    single week — trading a wrong number for a missing one. The caller now
    invokes this immediately after the canonical block.

    NO silent fallback to the other population: when canon is unavailable
    this drops to the lifetime sentence and publishes no weekly claim at
    all. A weekly number computed on an undeclared basis is the defect being
    fixed; publishing none beats publishing one nobody can trace.

    ★ REPOINTED AGAIN 2026-08-05 (moving baseline). The 08-05 repoint above
    fixed the POPULATION and left the WINDOW rolling: both halves of the
    sentence were anchored to now(). Three cache-busted reads minutes apart
    returned "served 6,764 (+73.3% WoW)", then "6,762 (+73.2%)", then 6,757
    — the level moved because the rolling window slid, and the percentage
    moved because real_external_calls_prior_7d recomputed under it
    (3,903 -> 3,905). A string designed to be quoted VERBATIM by press and by
    AI partners must return the same characters on two reads; this one could
    not, by construction.

    Bound instead to the fixed-window series shipped in PR #2260, whose own
    payload says it plainly: parity_rolling_7d "overlaps the in-progress
    week, so it moves between requests and must not be used as a week-over-
    week baseline — that is the defect this endpoint exists to fix". Same
    population (is_public_ip AND is_real_external — the series and the
    funnel headline differ only by window), so this is a window change, not
    a population change. The sentence now names the week it is about, and
    both numbers hold until that week is no longer the latest complete one.

    Note the level moved too, not just the percentage — so dropping the WoW
    and keeping the rolling level would NOT have produced a quotable
    sentence. The window had to be fixed for either half to hold still.
    """
    try:
        _ext = out.get("ai_agent_requests_external")
        try:
            if series is None:
                series = _press_series()
            _wk, _wow, _week_label = _fixed_window_claim(series)
        except Exception:
            # Degrade to the lifetime sentence. The outer handler would leave
            # press_headline_metric ABSENT, which reads to a consumer as "DC
            # Hub published nothing this week" rather than "no weekly claim".
            _wk = _wow = _week_label = None
        # Name the REASON the delta is missing. "no fixed baseline" would be a
        # false explanation when the baseline is perfectly fixed and it is the
        # POPULATION that moved — and a reader who cannot tell the two apart
        # will assume the series is broken rather than that it is being careful.
        # Two hazards, two sentences: a reader told "the definition changed
        # inside this window" about a delta whose weeks BOTH predate the
        # correction would go looking for a change that is not there.
        _crosses = _superseded = False
        try:
            _c = ((series or {}).get("wow") or {}).get("comparability") or {}
            _crosses = bool(_c.get("crosses_definition_change"))
            _superseded = bool(_c.get("superseded_by_correction"))
        except Exception:
            _crosses = _superseded = False
        _wow_s = (f"{_wow:+.1f}% WoW" if _wow is not None
                  else "WoW withheld — the counting definition changed inside "
                       "this window, so a delta across it is not a trend"
                  if _crosses else
                  "WoW withheld — every week in this delta predates a "
                  "measurement correction, so it describes a population that "
                  "has since been withdrawn, not a trend"
                  if _superseded else
                  "WoW withheld — no fixed baseline")
        _top = [p.get("name") or p.get("platform")
                for p in (out.get("ai_agent_top_platforms_external") or [])
                if (p.get("name") or p.get("platform"))][:2]
        _lead = f"led by {' and '.join(_top)} " if _top else ""
        # ★ 2026-09-02 — THE LEVEL WAS HONEST AND THE SENTENCE WAS NOT.
        # weeks[-1] carries top_caller_calls / top_caller_pct /
        # calls_net_of_top / top_caller_client and sets concentration_flag
        # when one caller is at or above CONCENTRATION_PCT of the week; this
        # renderer read `calls` and ignored the rest. Measured 2026-09-02
        # 00:23Z: "served 1,810 external AI-agent tool calls in the week of
        # 2026-08-24" — of which 1,473 (81.4%) were ONE caller (`chain-hire`,
        # one IP, one tool, no key) and 337 were everyone else. A sentence
        # built to be quoted verbatim must carry its own concentration; the
        # numbers are read off the same week row the level comes from and
        # never recomputed here (top_caller_calls + calls_net_of_top == calls
        # holds by construction in weekly_series).
        # Inline on purpose: the fence tests exec this function with only
        # PRESS_HEADLINE_BASIS and _fixed_window_claim bound, so a new
        # module-level helper would NameError inside this try and silently
        # degrade every headline to the lifetime sentence.
        _conc = ""
        # ★ 2026-09-02. The clause below said "came from a single caller
        # (chain-hire)" — true, and it reads as a large CUSTOMER. chain-hire
        # is a bulk harvester: two IPs, one tool (`search`), no api_key, a flat
        # 100-132 calls/hour for 14 hours, 1,410 of its calls served past the
        # anonymous cap. A sentence built to be quoted verbatim must not let a
        # journalist or a board read a scraper as demand, so when the week is
        # harvester-dominated the clause NAMES it as one and leads with the
        # net figure.
        #
        # Dominance is NOT re-derived here against a second threshold: it is
        # read off wow.comparability.harvester_dominated_weeks, the verdict
        # weekly_series already published (#3581/#3585). One gate, one answer.
        # Inline like everything else in this block — the fence tests exec this
        # function with only PRESS_HEADLINE_BASIS and _fixed_window_claim
        # bound, so a module-level helper would NameError and silently degrade
        # every headline to the lifetime sentence.
        _harv_weeks = set()
        try:
            for _hw in (((series or {}).get("wow") or {}).get("comparability")
                        or {}).get("harvester_dominated_weeks") or []:
                if isinstance(_hw, dict) and _hw.get("week_start"):
                    _harv_weeks.add(_hw["week_start"])
        except (AttributeError, TypeError):
            _harv_weeks = set()
        try:
            for _w in ((series or {}).get("weeks") or []):
                if not (isinstance(_w, dict)
                        and _w.get("week_start") == _week_label):
                    continue
                if (_week_label in _harv_weeks
                        and _w.get("harvester_calls") is not None):
                    _hc = int(_w["harvester_calls"])
                    _hnet = _w.get("calls_net_of_harvesters")
                    _hnet = (int(_hnet) if _hnet is not None
                             else max(int(_wk) - _hc, 0))
                    _hpct = _w.get("harvester_pct")
                    _hpct_s = (f"{float(_hpct):.0f}%" if _hpct is not None
                               else f"{100.0 * _hc / max(int(_wk), 1):.0f}%")
                    # Name the one that actually ran when we can (the top
                    # caller, if it is a known harvester); otherwise the class.
                    _top = str(_w.get("top_caller_client") or "")
                    _names = [str(n) for n in (_w.get("harvester_names") or [])]
                    _who = (_top if _top and _top in _names
                            else (", ".join(_names) if _names else "a bulk harvester"))
                    _conc = (f", of which {_hc:,} ({_hpct_s}) were a BULK "
                             f"HARVESTER ({_who} — one tool, no API key) and "
                             f"NOT demand; {_hnet:,} came from all other "
                             f"callers")
                    break
                if _w.get("concentration_flag") and _w.get("top_caller_calls") is not None:
                    _tc = int(_w["top_caller_calls"])
                    _net = _w.get("calls_net_of_top")
                    _net = (int(_net) if _net is not None
                            else max(int(_wk) - _tc, 0))
                    _pct = _w.get("top_caller_pct")
                    _pct_s = (f"{float(_pct):.0f}%" if _pct is not None
                              else f"{100.0 * _tc / max(int(_wk), 1):.0f}%")
                    _who = str(_w.get("top_caller_client") or "unidentified")
                    _conc = (f", of which {_tc:,} ({_pct_s}) came from a "
                             f"single caller ({_who}); {_net:,} from all others")
                break
        except (TypeError, ValueError):
            _conc = ""
        if _wk is not None and _ext:
            out["press_headline_metric"] = (
                f"DC Hub served {_wk:,} external AI-agent tool calls "
                f"in the week of {_week_label} ({_wow_s}){_conc}; {_ext:,} "
                f"external requests {_lead}since launch."
            )
            _harv_rendered = "BULK HARVESTER" in _conc
            out["press_headline_metric_basis"] = PRESS_HEADLINE_BASIS + (
                (" ★ Harvester clause: weeks[-1].harvester_calls / "
                 "harvester_pct / calls_net_of_harvesters / harvester_names, "
                 "read off the SAME week row as the level. Rendered only when "
                 "that week appears in "
                 "wow.comparability.harvester_dominated_weeks — the verdict "
                 "weekly_series already published, NOT a threshold this "
                 "renderer owns, so there is one gate and one answer. A "
                 "harvester is a NAMED tag measured as one caller taking one "
                 "tool at machine cadence holding no api_key "
                 "(mcp_calls_deloop.HARVESTER_PLATFORMS); it is reported "
                 "INSIDE the level and subtracted beside it, never excluded — "
                 "no week is restated. harvester_calls + "
                 "calls_net_of_harvesters == calls. This clause SUPERSEDES "
                 "the concentration clause when both would apply: 'a single "
                 "caller' reads as a customer, and a scraper is not one."
                 if _harv_rendered else
                 " ★ Concentration clause: weeks[-1].top_caller_calls / "
                 "top_caller_pct / calls_net_of_top / top_caller_client, read "
                 "off the SAME week row as the level and rendered only when "
                 "weeks[-1].concentration_flag is true (top caller >= "
                 "CONCENTRATION_PCT of the week's calls); top_caller_calls + "
                 "calls_net_of_top == calls. The caller is named by its "
                 "client_name/platform; whether it held a key is not in the "
                 "row.")
                if _conc else "")
        elif _ext:
            out["press_headline_metric"] = (
                f"DC Hub has served {_ext:,} external AI-agent requests "
                f"{_lead}since launch."
            )
            out["press_headline_metric_basis"] = (
                "lifetime external requests only — no weekly claim. The "
                "fixed-window weekly figure (weekly-series weeks[-1]) was "
                "unavailable, and this sentence will not substitute a "
                "different population or a rolling window for it — a "
                "quotable string with no weekly claim beats one whose "
                "numbers change between two reads."
            )
        elif out.get("ai_agent_requests_total"):
            _t = out["ai_agent_requests_total"]
            out["press_headline_metric"] = (
                f"DC Hub has served {_t:,} AI-agent requests since launch."
            )
            out["press_headline_metric_basis"] = (
                "lifetime ALL-SOURCES requests — internal and self-heal "
                "traffic NOT excluded, and no weekly claim. Last-resort "
                "sentence: both the canonical weekly figure and the "
                "external lifetime figure were unavailable."
            )
    except Exception:
        pass


# ── GET /api/v1/mcp/funnel — Public aggregate stats for the dashboard ─────

@mcp_bp.get("/api/v1/mcp/funnel")
def mcp_funnel():
    # Phase FF+25-followup-r3 (2026-05-20) — split probe vs real traffic.
    # Until now `tool_calls_7d` lumped together genuine MCP-client traffic
    # AND our own QA / healer probes (User-Agent matches python-script,
    # node-script, curl, postman, insomnia, plus the always-unattributed
    # "unknown" bucket). When CF WAF temporarily over-blocked our probes,
    # the 7d number dropped 38k→27k and looked like real-user churn even
    # though zero external clients had changed behavior.
    #
    # `tool_calls_7d_real` excludes those self-traffic platforms so the
    # public dashboard can show what AI agents are actually doing. Both
    # numbers ship in the response — `tool_calls_7d` stays as the gross
    # count for backward compat (brain detectors / mcp_growth.py still
    # read it) and `tool_calls_7d_real` is what the UI should highlight.
    # r65-qa (#3): 'unknown' REMOVED — it was zeroing out tool_calls_7d_real.
    # The CF worker strips the client UA so legit external MCP traffic falls to
    # 'unknown'; lumping it with probes classified 100% of 7d traffic as probe
    # → real KPI structurally read 0. Per this list's own sibling comment below,
    # 'unknown' is real external traffic we couldn't sub-classify, NOT a probe.
    #
    # 2026-06-19: _PROBE_PLATFORMS now comes from the shared mcp_calls_deloop
    # module so this endpoint's `tool_calls_7d_real` is byte-identical to
    # routes/funnel_health's. The shared list ADDS 'internal-dchub' +
    # 'node-http-client' (our own crawler/healer UAs fold into 'internal-dchub'
    # via PLATFORM_CASE) — previously those self-calls leaked into the "real"
    # count here while funnel_health excluded them, the exact drift this fixes.
    _PROBE_PLATFORMS = _DELOOP_PROBE_PLATFORMS
    # r61: internal/self + registry-scanner client names that crush the
    # funnel's per-platform conversion rate to ~0% (they emit signals but
    # never convert — see reference_dchub_mcp_signal_inflation). Excluded
    # from signals_by_platform_30d so the funnel reflects EXTERNAL agent
    # demand. Pattern families (loop*, dchub-*, *-probe/-health/-scanner/
    # -checker, local-agent-mode*) are matched separately via NOT LIKE.
    # 'unknown' is intentionally NOT excluded here — it's real external
    # traffic we couldn't sub-classify, not self-traffic.
    _INTERNAL_PLATFORMS = (
        'node', 'dchub-selfheal', 'dchub-mcp-test', 'mcp-probe', 'mcp-test',
        'pipeline_mcp', 'canary', 'mcp-remote-fallback-test',
        'registry-health-checker', 'mcp-shield-scanner', 'yellowmcp-health',
        'glama-health', 'chiark-prober', 'fabrique-noauth-probe',
        'agentpulse', 'mcpscoringengine', 'mcp-extractor',
        'curl', 'python-script', 'node-script', 'postman', 'insomnia', 'verify',
    )
    _excl_in = ",".join(
        "'" + str(p).replace("'", "''") + "'" for p in sorted(set(_INTERNAL_PLATFORMS)))
    # No bound params in the signals query, so literal % in LIKE is safe.
    _signal_excl_clause = (
        f" AND COALESCE(LOWER(mcp_client),'') NOT IN ({_excl_in}) "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE 'loop%' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE 'dchub-%' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE 'local-agent-mode%' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE 'leakaudit%' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE 'trial-leak%' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE '%-probe' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE '%-health' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE '%-scanner' "
        " AND COALESCE(LOWER(mcp_client),'') NOT LIKE '%-checker' "
    )
    out = {}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Item E-selfheal (2026-06-02): lazy CREATE VIEW mcp_funnel_real
            # if missing. Mirrors routes/schema_repair.py:272 (the canonical
            # is_synthetic=FALSE view). Wrapped in a broad try/except so a
            # CREATE failure (permissions, missing base table, etc.) does
            # NOT 500 the funnel endpoint — readers downstream still degrade
            # to real_external_7d = None per the Item E fallback pattern.
            # Idempotent: to_regclass() check + CREATE OR REPLACE.
            try:
                cur.execute("SELECT to_regclass('public.mcp_funnel_real')")
                _exists = (cur.fetchone() or [None])[0]
                if _exists is None:
                    # is_synthetic gate identical to schema_repair.py:241-270.
                    # We use a single CREATE OR REPLACE so concurrent callers
                    # don't race; if the canonical view exists, we reuse it
                    # rather than rebuilding the upstream mcp_funnel_canonical.
                    cur.execute("""
                        CREATE OR REPLACE VIEW mcp_funnel_real AS
                            SELECT
                              s.id, s.created_at, s.signal_type, s.tool_requested,
                              s.tier_current, s.tier_required,
                              s.session_id, s.user_email, s.ip_address,
                              s.mcp_client, s.user_agent,
                              s.converted, s.converted_at,
                              s.outreach_sent, s.outreach_sent_at
                            FROM mcp_upgrade_signals s
                            WHERE NOT (
                                COALESCE(LOWER(s.mcp_client),'') IN (
                                  'node','dchub-selfheal','dchub-mcp-test',
                                  'mcp-probe','mcp-test','pipeline_mcp','canary',
                                  'mcp-remote-fallback-test',
                                  'registry-health-checker','mcp-shield-scanner',
                                  'yellowmcp-health','glama-health','chiark-prober',
                                  'fabrique-noauth-probe','agentpulse',
                                  'mcpscoringengine','mcp-extractor',
                                  'curl','python-script','node-script',
                                  'postman','insomnia','verify',
                                  'sweep','diag','audit','gating-audit','devin',
                                  't','p','v','fv','test','internal-dchub'
                                )
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'loop%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'dchub-%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'local-agent-mode%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'leakaudit%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'trial-leak%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%-probe'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%-health'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%-scanner'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%-checker'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'sweep%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'diag%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'audit%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'anon-seed%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'cap-%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'adv-%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%-test'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'test-%%'
                                OR COALESCE(LOWER(s.mcp_client),'') IN ('probe','eval')
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE 'curl%%'
                                OR COALESCE(LOWER(s.mcp_client),'') LIKE '%%verify%%'
                                OR LENGTH(COALESCE(s.mcp_client,'')) <= 2
                            )
                    """)
                    conn.commit()
            except Exception as _ve:
                # Never block the funnel on view-creation failure; the
                # downstream try/except for real_external_7d already
                # degrades cleanly to None.
                try: conn.rollback()
                except Exception: pass
                try:
                    import logging as _lg
                    _lg.getLogger(__name__).warning(
                        "mcp_funnel lazy view create skipped: %s", _ve)
                except Exception:
                    pass

            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d"] = cur.fetchone()[0]

            # r89g (2026-06-15): complete-days 7d count, kept for back-compat.
            # ★ CORRECTED 2026-08-05 — this comment used to say the rolling
            # tool_calls_7d "dips mid-day" because it "INCLUDES the in-progress
            # current UTC day". That is WRONG, and it sent three later readers
            # (including the top-caller block below) chasing a window bug that
            # does not exist. A rolling `created_at >= now() - interval '7 days'`
            # window is ALWAYS exactly 168h wide — it is not truncated at either
            # end, it is PHASE-SHIFTED relative to the complete-days window,
            # which is also exactly 168h wide. Neither is partial. Empirically
            # (2026-08-05): tool_calls_7d 8,523 vs tool_calls_7d_complete 8,480
            # — 0.5% apart, which is the phase shift, not a mid-day collapse.
            # When these two disagree materially, the cause is BASIS (different
            # table / identity column / exclusion predicate), not window width.
            # tool_calls_7d_complete sums the last 7 COMPLETE days (excludes
            # today); per_day is the daily rate. tool_calls_7d stays as-is for
            # back-compat (brain detectors / mcp_growth.py read it).
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' "
                "  AND created_at < CURRENT_DATE"
            )
            _cd7 = int((cur.fetchone() or [0])[0])
            out["tool_calls_7d_complete"] = _cd7
            out["tool_calls_per_day_complete"] = round(_cd7 / 7)

            # r-funnel-platform-excl (2026-06-20): the de-looped, partial-day-robust
            # HEADLINE number. complete-7-days (no mid-day dip) AND external-only —
            # the shared de-loop predicate now also drops self-heal/probe traffic by
            # the write-time platform column, so a self-heal cadence change can no
            # longer read as a funnel collapse (the recurring false alarm). The
            # dashboard headline points at THIS field; tool_calls_7d_complete stays
            # for back-compat.
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls "
                    "WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' "
                    "  AND created_at < CURRENT_DATE "
                    f"  AND {_deloop_real_calls_predicate()}"
                )
                _cd7r = int((cur.fetchone() or [0])[0])
                out["tool_calls_7d_complete_real"] = _cd7r
                out["tool_calls_per_day_complete_real"] = round(_cd7r / 7)
            except Exception as e:
                out["tool_calls_7d_complete_real"] = None
                out["tool_calls_7d_complete_real_error"] = str(e)[:120]

            # brain-l15 #1656/#1661 (2026-07-18): WoW context so cumulative
            # headlines can't mask a cliff. PRIOR complete-7d window (days
            # -14..-7, same de-loop predicate) + week-over-week % for the
            # external headline — the trend the lifetime counter structurally
            # hides (pct_7d_of_lifetime=0.2% only says "spike ended"; this
            # says whether THIS week is up or down vs last).
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls "
                    "WHERE created_at >= CURRENT_DATE - INTERVAL '14 days' "
                    "  AND created_at < CURRENT_DATE - INTERVAL '7 days' "
                    f"  AND {_deloop_real_calls_predicate()}"
                )
                _pd7r = int((cur.fetchone() or [0])[0])
                out["tool_calls_prior_7d_complete_real"] = _pd7r
                _cur7 = out.get("tool_calls_7d_complete_real")
                out["tool_calls_wow_pct"] = (
                    round(100.0 * (_cur7 - _pd7r) / _pd7r, 1)
                    if (_cur7 is not None and _pd7r) else None)
                # ★ 2026-08-20 — the 5th of the seven flat *_wow_pct keys, and
                # the one still RENDERING after #2978/#2980: the dashboard's
                # TOOL CALLS card printed "WoW -19.7%". Both windows are built
                # on _deloop_real_calls_predicate(), which is exactly what
                # dchub-mcp-server#202 changed, and the current window
                # [CURRENT_DATE-7d, CURRENT_DATE) contains 2026-08-18.
                # Date-anchored, not NOW()-anchored: this pair is the
                # complete-DAYS variant, so _week_spans over the two start
                # dates reproduces its exact half-open bounds.
                from datetime import date as _d, timedelta as _tdd
                _t0 = _d.today()
                _mark_wow_comparability(
                    out,
                    _week_spans([_t0 - _tdd(days=14), _t0 - _tdd(days=7)]),
                    ("tool_calls_wow_pct",), "tool_calls_complete_days")
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["tool_calls_prior_7d_complete_real"] = None
                out["tool_calls_wow_pct"] = None

            # r-topcaller (2026-07-18): single-caller concentration shown next
            # to the headline. One anonymous AWS bot ramped to 45% of a day's
            # external calls (07-12→07-16) then vanished mid-burst — the
            # headline read it as a funnel decline. A trend over a number one
            # caller can dominate must show that concentration next to it.
            #
            # ★ REPOINTED 2026-08-05 (basis alignment). This block used to run
            # its OWN query — mcp_tool_calls, grouped by ip_address, over the
            # complete-days window — and divide by ITS OWN denominator (7,159).
            # The card renders the result on the SAME LINE as the headline,
            # which is real_external_calls_7d (mcp_calls_identity, agent_id,
            # rolling — 6,705). So the card printed "6,705 … top caller 34.6%
            # (2,478 calls)" while 2,478/6,705 is 37.0%: one line contradicting
            # itself, and any reader doing the division caught it. Measured
            # 2026-08-05 the denominator gap was 453 calls — 143 (31.6%) window
            # phase, 310 (68.4%) BASIS — so it was never fixable by nudging the
            # window. canonical_top_caller_sql emits numerator AND denominator
            # from ONE query over the same rows as the headline, so the printed
            # percentage now equals the division a reader performs. The
            # numerator excludes the NULL agent_id bucket (Cloudflare POPs are
            # edge proxies, not a caller); the denominator does not, so it stays
            # byte-equal to real_external_calls_7d. Verified 2026-08-05 that the
            # NULL bucket was not the max (0 rows in window) before adopting it.
            try:
                cur.execute(_canonical_top_caller_sql(7))
                _tc = cur.fetchone() or (0, 0, 0, 0, 0)
                # SUM() comes back Decimal — cast before mixing with floats.
                _topc, _totc, _ips = (int(_tc[0] or 0), int(_tc[1] or 0),
                                      int(_tc[2] or 0))
                # r-net-of-top (2026-08-24): columns 4/5 are additive — indexed
                # defensively so a replica still serving the 3-column shape
                # mid-deploy degrades to "field absent" instead of IndexError
                # taking the whole top-caller block into its except and
                # nulling four already-published fields.
                _netc = int(_tc[3] or 0) if len(_tc) > 3 else None
                _neta = int(_tc[4] or 0) if len(_tc) > 4 else None
                _pct = round(100.0 * _topc / _totc, 1) if _totc else None
                # Correctly-named fields: this triple is ROLLING now (it comes
                # from the same rolling query as the headline), so the old
                # "_complete" suffix no longer describes it. New names below,
                # old names kept as aliases because /api/v1/mcp/funnel is
                # public and may have readers outside this repo.
                out["top_caller_calls_7d"] = _topc
                out["top_caller_pct_7d"] = _pct
                out["external_agents_7d"] = _ips
                # The denominator this percentage was actually taken over, so
                # a reader never has to guess which figure to divide by.
                out["top_caller_denominator_7d"] = _totc
                # ★ 2026-08-10: NAME the top caller. mcp_calls_deloop's
                # _AMBIGUOUS_NOT_EXCLUDED deliberately keeps smithery/glama/
                # agent-toolscloud in the population, and justifies it as "a
                # slightly generous count WE CAN STILL SEE AND NAME". Nothing
                # named it — the dashboard published "top caller 41% of external
                # calls" with no identity, so a registry prober read as a
                # customer-concentration risk.
                #
                # Measured 2026-08-10: the top caller was ONE agent_id,
                # client_name 'Smithery Connect', user_agent 'node' — 3,215
                # calls over 45 tools in 30d, with claim_free_key called 321
                # times. Excluding it was NOT the fix (that block is right that
                # a false exclusion is worse); labelling it is.
                try:
                    cur.execute("""
                        SELECT COALESCE(NULLIF(client_name,''), platform, 'unknown'),
                               COALESCE(platform,''), COUNT(*) n
                          FROM mcp_calls_identity
                         WHERE is_public_ip AND is_real_external
                           AND created_at >= NOW() - INTERVAL '7 days'
                           AND agent_id IS NOT NULL
                         GROUP BY agent_id, 1, 2
                         ORDER BY n DESC LIMIT 1""")
                    _tcn = cur.fetchone()
                    if _tcn:
                        out["top_caller_client"] = _tcn[0]
                        out["top_caller_platform"] = _tcn[1] or None
                        out["top_caller_note"] = (
                            "Registry gateways and directory probes are "
                            "deliberately NOT excluded from this population "
                            "(mcp_calls_deloop._AMBIGUOUS_NOT_EXCLUDED) because "
                            "a false exclusion would delete a real customer. "
                            "Read the client name before treating this share as "
                            "customer concentration.")
                except Exception:
                    pass
                out["top_caller_basis"] = _CANONICAL_TOP_CALLER_BASIS

                # ★★★ r-net-of-top (2026-08-24). THE MISSING HALF OF THE
                # CONCENTRATION STORY.
                #
                # Measured this morning: top_caller_pct_7d = 90.4 (2,192 of
                # 2,425), the caller being 'Smithery Connect' on ONE IP — while
                # real_external_agents_7d fell 72 -> 16 because mcp-server #202
                # correctly reclassified our own GitHub Actions suites as
                # internal. Two artifacts, opposite signs, in one card: the
                # owner read the page as "the MCP funnel continues to decline"
                # when calls had in fact risen 14.4%, all of the rise inside one
                # gateway. Both numbers on the card were TRUE and the reading
                # was still wrong, because the card published a share without
                # ever publishing its remainder.
                #
                # The fix is not to exclude the caller — mcp_calls_deloop.
                # _AMBIGUOUS_NOT_EXCLUDED is right that a false exclusion
                # silently deletes a real customer, and Smithery is a hosted
                # gateway that may proxy real users. The fix is to publish the
                # subtraction the share already implies, from the SAME query,
                # so nobody does it by hand across two payload fields and hopes
                # the bases match. That hand-subtraction across mismatched
                # bases is the exact defect r-basis-align fixed in 08-05.
                out["demand_net_of_top_caller_7d"] = {
                    "calls":              _netc,
                    "agents":             _neta,
                    "headline_calls":     _totc,
                    "headline_agents":    _ips,
                    "top_caller_client":  out.get("top_caller_client"),
                    "top_caller_calls":   _topc,
                    "top_caller_pct":     _pct,
                    # TRUE => the headline tracks one caller, per lane 5 of
                    # agent-retention-master-shell#49, which now gates on the
                    # same single-sourced threshold.
                    "concentration_flag": (
                        _pct is not None and _pct >= _CONCENTRATION_PCT),
                    "concentration_threshold_pct": _CONCENTRATION_PCT,
                    "identity": (
                        "headline_calls == top_caller_calls + calls — holds by "
                        "construction; all four come from one query over one "
                        "window"),
                    "basis": _CANONICAL_NET_BASIS,
                }

                # Back-compat aliases (same values, now same basis).
                out["external_ips_7d_complete"] = _ips
                out["top_caller_calls_7d_complete"] = _topc
                out["top_caller_pct_7d_complete"] = _pct
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["top_caller_calls_7d"] = None
                out["top_caller_pct_7d"] = None
                out["external_agents_7d"] = None
                out["top_caller_denominator_7d"] = None
                out["top_caller_basis"] = None
                out["external_ips_7d_complete"] = None
                out["top_caller_calls_7d_complete"] = None
                out["top_caller_pct_7d_complete"] = None
                out["demand_net_of_top_caller_7d"] = None

            # ★ 2026-09-01 (r-harvester-split). demand_net_of_top_caller_7d
            # answers "how much of the headline is ONE caller?" — it cannot
            # answer "how much of it is a caller that is not demand at all?".
            # On this date those were the same row and the coincidence hid the
            # gap: `chain-hire` was BOTH the top caller AND a bulk harvester
            # (one IP, one tool, no api_key, 1,410 calls served past the
            # anonymous cap at a flat 100-132/hour for 14 hours), so
            # net-of-top happened to read like net-of-harvester. The moment a
            # real gateway is the top caller and a harvester sits second, the
            # two diverge and only this block is still true.
            #
            # It is a DECOMPOSITION, not an exclusion: is_real_external is
            # untouched, so no already-published week is restated. See
            # mcp_calls_deloop.HARVESTER_PLATFORMS for why that is deliberate.
            try:
                cur.execute(_canonical_harvester_split_sql(7))
                _hs = cur.fetchone() or (0, 0, 0, 0, 0, 0)
                _h_calls, _h_hcalls, _h_netcalls = (int(_hs[0] or 0),
                                                    int(_hs[1] or 0),
                                                    int(_hs[2] or 0))
                _h_agents, _h_hagents, _h_netagents = (int(_hs[3] or 0),
                                                       int(_hs[4] or 0),
                                                       int(_hs[5] or 0))
                _h_pct = (round(100.0 * _h_hcalls / _h_calls, 1)
                          if _h_calls else None)
                out["demand_net_of_harvesters_7d"] = {
                    "calls":               _h_netcalls,
                    "agents":              _h_netagents,
                    "headline_calls":      _h_calls,
                    "headline_agents":     _h_agents,
                    "harvester_calls":     _h_hcalls,
                    "harvester_agents":    _h_hagents,
                    "harvester_pct":       _h_pct,
                    # Named, not counted-and-hidden. A reader can check any
                    # one of these against calls_by_platform_30d, where the
                    # same names carry kind='harvester'.
                    "harvesters_named":    sorted(_HARVESTER_PLATFORMS),
                    "excluded_from_headline": False,
                    "identity": (
                        "headline_calls == harvester_calls + calls — holds by "
                        "construction (complementary FILTERs on one scan). "
                        "AGENTS DO NOT SUM: an agent_id with both harvester "
                        "and non-harvester rows is counted in headline_agents, "
                        "harvester_agents AND agents, so those three are three "
                        "populations, not a partition"),
                    "basis": _CANONICAL_HARVESTER_BASIS,
                }
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["demand_net_of_harvesters_7d"] = None

            # ★ 2026-08-05: the #2254 rename landed in the SOURCE, but a reader
            # of this endpoint gets JSON, not comments — and from the JSON
            # alone `external_ips_7d_complete` is indistinguishable from a
            # genuine complete-day metric. An audit this week flagged it for
            # exactly that: it is byte-identical to the ROLLING
            # real_external_agents_7d (both read 47 on 2026-08-05) and has no
            # `_prior` sibling, so a reader who trusted the suffix would pair a
            # rolling value with a complete-day baseline from somewhere else
            # and publish a delta spanning two different windows. The suffix is
            # the last surviving piece of the pre-#2254 basis. Deleting the
            # keys would break the outside readers they exist for, so the
            # deprecation is NAMED IN THE PAYLOAD instead — checkable by a
            # machine, not just by whoever opens this file.
            #
            # Emitted OUTSIDE the try above on purpose: it is static
            # documentation, not a measurement. On a query failure the three
            # aliased keys render null, and that is exactly when a reader most
            # needs to be told which key supersedes which.
            out["deprecated_aliases"] = {
                "external_ips_7d_complete": "external_agents_7d",
                "top_caller_calls_7d_complete": "top_caller_calls_7d",
                "top_caller_pct_7d_complete": "top_caller_pct_7d",
            }
            out["deprecated_aliases_note"] = (
                "These keys are unchanged in VALUE and kept only so existing "
                "readers do not break. Their '_complete' suffix is "
                "INACCURATE: since PR #2254 they are computed on the rolling "
                "7d window ending now, NOT on complete days, and none of them "
                "has a _prior sibling to form a delta against. Read the "
                "replacement key named here. For a window whose baseline does "
                "not move between requests, use GET "
                "/api/v1/reports/weekly-series — fixed, non-overlapping, "
                "complete ISO weeks only."
            )

            # r42x (2026-05-26): lifetime aggregate so press releases can
            # cite "N total tool calls since launch" as a moat metric.
            #
            # ★ 2026-09-03: this read pg_class.reltuples FIRST — a PLANNER
            # STATISTIC, updated by VACUUM / ANALYZE / CREATE INDEX and never
            # by INSERT. Between autovacuum runs it does not move at all, no
            # matter how many calls arrive. Measured that day: two snapshots
            # eight hours apart both published exactly 260,147 while the 7d
            # rolling window over the SAME events moved — roughly nine calls
            # an hour that the "all-time" figure denied had happened.
            #
            # The guards were `== 0` and `< tool_calls_7d`. Those catch a
            # never-analyzed table and an absurdly low estimate. Staleness
            # trips NEITHER — a stale total is neither zero nor small — so the
            # approximation published as an exact number, to the unit, in the
            # one field whose own comment invites press to quote it.
            #
            # mcp_tool_calls is ~260k rows and this cursor already runs several
            # COUNT(*)s over it. Count it. A read that fails reports that it
            # failed; it does not hand back an estimate and let the reader
            # believe the number was counted.
            try:
                cur.execute("SELECT COUNT(*) FROM mcp_tool_calls")
                out["tool_calls_total"] = int((cur.fetchone() or [0])[0] or 0)
                out["tool_calls_total_basis"] = "COUNT(*) mcp_tool_calls — exact"
            except Exception:
                out["tool_calls_total"] = None
                out["tool_calls_total_basis"] = "unavailable — the count failed"

            # 30-day window — useful for monthly press cadence
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at >= NOW() - INTERVAL '30 days'"
                )
                out["tool_calls_30d"] = int((cur.fetchone() or [0])[0])
            except Exception:
                out["tool_calls_30d"] = None

            # r42y (2026-05-26): pull the TRUE lifetime number from the
            # ai_cumulative aggregate table — same source as the public
            # /api/public/mcp-count widget (389K+ across all platforms,
            # not just the 126K mcp_tool_calls row count). Press copy
            # should cite ai_agent_requests_total as the headline.
            try:
                cur.execute("SELECT COALESCE(SUM(total_requests), 0) FROM ai_cumulative")
                out["ai_agent_requests_total"] = int((cur.fetchone() or [0])[0])
            except Exception:
                out["ai_agent_requests_total"] = None

            # brain-l15 #1439/#1447 (2026-07-05): the all-sources total above is
            # ~68% internal/self-heal/probe traffic (platform='internal' alone was
            # ~1.08M of ~1.6M). The /ai dashboard deliberately leads with the
            # all-sources number, so we KEEP it unchanged — but expose an
            # EXTERNAL-only figure that drops our own internal/self-heal/probe
            # platforms, and point the PRESS headline (below) at THAT. Press copy
            # must never claim "led by Claude and ChatGPT" over a number that is
            # mostly our own self-heal traffic.
            # 2026-07-18: the denylist above still let transport buckets top the
            # "external" ranking — 'direct' (~143K) and 'mcp' (~103K) outranked
            # Claude, so the press headline read "led by Direct and Claude".
            # 'direct'/'mcp'/'mcp_generic' are how a request arrived, not an
            # external AI platform; junk rows (glama, unknown_ai, *-health,
            # scanners) leaked too. Gate on the AI_PLATFORMS allowlist instead —
            # the same source as _is_real_ai_platform in /api/v1/ai-tracking/stats
            # (HONEST NUMBERS #61 / r71) — with the hardened denylist as the
            # fail-open fallback if the import is unavailable.
            _aicum_excl = (
                "COALESCE(LOWER(platform),'') NOT IN "
                "('internal','internal-dchub','dchub-selfheal','dchub-regression-test',"
                "'dchub-mcp-test','mcp-test','mcp-probe','probe','value-harness','node',"
                "'node-script','python-script','curl','postman','insomnia','verify',"
                "'direct','mcp','mcp_generic','unknown_ai') "
                "AND COALESCE(LOWER(platform),'') NOT LIKE 'dchub-%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%probe%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%harness%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%-test' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE 'test-%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%regression%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%selfheal%' "
                "AND COALESCE(LOWER(platform),'') NOT LIKE '%audit%'"
            )
            try:
                from ai_tracking import AI_PLATFORMS as _aicum_allow
                if _aicum_allow:
                    _aicum_excl = (
                        "COALESCE(LOWER(platform),'') IN ("
                        + ",".join("'%s'" % k for k in sorted(_aicum_allow))
                        + ")"
                    )
            except Exception:
                pass
            try:
                cur.execute(
                    f"SELECT COALESCE(SUM(total_requests), 0) FROM ai_cumulative "
                    f"WHERE {_aicum_excl}"
                )
                out["ai_agent_requests_external"] = int((cur.fetchone() or [0])[0])
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["ai_agent_requests_external"] = None

            try:
                cur.execute(
                    "SELECT platform, name, total_requests FROM ai_cumulative "
                    "ORDER BY total_requests DESC LIMIT 10"
                )
                out["ai_agent_top_platforms"] = [
                    {"platform": r[0], "name": r[1], "requests": int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception:
                out["ai_agent_top_platforms"] = []

            # External-only ranking (internal/self-heal/probe removed) — the list
            # press + external surfaces should quote instead of the raw one, where
            # 'internal' was the #1 "platform".
            try:
                cur.execute(
                    f"SELECT platform, name, total_requests FROM ai_cumulative "
                    f"WHERE {_aicum_excl} "
                    f"ORDER BY total_requests DESC LIMIT 10"
                )
                out["ai_agent_top_platforms_external"] = [
                    {"platform": r[0], "name": r[1], "requests": int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["ai_agent_top_platforms_external"] = []

            # Pre-calculated acceleration fields for press copy. Ratio
            # of recent-to-lifetime is the genuine moat story.
            try:
                _ai_total = out.get("ai_agent_requests_total") or out.get("tool_calls_total") or 0
                _30 = out.get("tool_calls_30d") or 0
                _7 = out.get("tool_calls_7d") or 0
                if _ai_total > 0 and _30 > 0:
                    out["pct_30d_of_lifetime"] = round(100.0 * _30 / _ai_total, 1)
                    out["pct_7d_of_lifetime"]  = round(100.0 * _7  / _ai_total, 1)
                    out["annualized_run_rate_from_7d"] = int(_7 * 52)
                else:
                    out["pct_30d_of_lifetime"] = None
                    out["pct_7d_of_lifetime"]  = None
                    out["annualized_run_rate_from_7d"] = None
                # ★ MOVED 2026-08-05: press_headline_metric is no longer built
                # here. It binds real_external_calls_7d, which is not computed
                # until ~500 lines below this point — building it here would
                # read None off `out` and silently degrade every week to the
                # lifetime-only sentence. See _build_press_headline(), called
                # immediately after the canonical block.
            except Exception:
                pass

            cur.execute(
                "SELECT COUNT(*) FROM mcp_upgrade_signals WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["upgrade_signals_7d"] = cur.fetchone()[0]

            # Item E (2026-06-02): expose the real external UPGRADE-SIGNAL
            # count alongside the raw count. Sourced from mcp_funnel_real
            # (the canonical is_synthetic=FALSE view shipped in 3704c21f /
            # schema_repair.py). Visitor-intel renders "Total signals: {raw}
            # · External verified: {real}" so both numbers are visible and
            # the signal-inflation gap is honest. Falls back to None if the
            # view is missing (idempotent — no schema-repair required).
            #
            # ★ RENAMED 2026-08-05 (population collision). These fields
            # shipped as real_external_7d / _prior_7d / _wow_pct — four
            # characters from real_external_calls_7d, with NO basis string
            # anywhere in the payload. They are not a call count and never
            # were: mcp_funnel_real is a VIEW over mcp_upgrade_signals, and
            # main.py runs this exact query under the name
            # `2_paywall_hits_7d`. On 2026-08-05 one payload carried calls
            # 6,868 (+87.4% WoW) beside signals 1,566 (-13.7% WoW) — 4.5x
            # apart, OPPOSITE signs, near-identical names. Whichever a board
            # happened to bind decided whether the week read as doubling or
            # shrinking. The trap was the NAME, so the name is gone rather
            # than aliased: a field that does not exist cannot be bound by
            # mistake. Both live readers (brain_micro_cycle, brain_layer9)
            # move in this same commit; static/mcp-dashboard.html and the
            # dchub-frontend repo were grepped and never read it.
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_funnel_real "
                    "WHERE created_at >= NOW() - INTERVAL '7 days'"
                )
                out["real_external_signals_7d"] = int((cur.fetchone() or [0])[0])
                out["real_external_signals_basis"] = _CANONICAL_SIGNALS_BASIS
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["real_external_signals_7d"] = None
                out["real_external_signals_basis"] = None

            # brain-l15 #1656/#1661: prior-7d + WoW for the upgrade-signal
            # figure too, so it carries its own trend context.
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_funnel_real "
                    "WHERE created_at >= NOW() - INTERVAL '14 days' "
                    "  AND created_at < NOW() - INTERVAL '7 days'"
                )
                out["real_external_signals_prior_7d"] = int((cur.fetchone() or [0])[0])
                _re7 = out.get("real_external_signals_7d")
                _rp7 = out["real_external_signals_prior_7d"]
                out["real_external_signals_wow_pct"] = (
                    round(100.0 * (_re7 - _rp7) / _rp7, 1)
                    if (_re7 is not None and _rp7) else None)
                # ★ 2026-08-20 — the 6th flat key, and the one it took reading
                # the VIEW to establish. mcp_funnel_real excludes
                # `mcp_client LIKE 'dchub-%'`, so post-#202 CI signals
                # (tagged 'dchub-internal') fall OUT of it — while pre-#202 the
                # same traffic wrote the generic 'mcp', which this view does
                # NOT exclude (not in the IN-list, matches no LIKE, length > 2).
                # So the signal population changed at the same instant and for
                # the same reason: measured -51.4% across it (1,105 -> 537).
                # NOT assumed from the drop — the drop is consistent with the
                # mechanism, and the mechanism is what justifies withholding.
                _mark_wow_comparability(
                    out, _rolling_spans(7, 2),
                    ("real_external_signals_wow_pct",),
                    "real_external_signals")
            except Exception:
                try: conn.rollback()
                except Exception: pass
                out["real_external_signals_prior_7d"] = None
                out["real_external_signals_wow_pct"] = None

            # r-wall-metrics (2026-08-10): quota-wall activity, surfaced
            # beside the upgrade signals it belongs with. The monthly wall
            # went enforcing 2026-08-08 (MONTHLY_QUOTA_ENFORCE=1) and its
            # allowed=false decisions are the conversion event it was built
            # to produce — until this block, nobody could see when it
            # started firing. Sourced from the mcp_quota_wall_hits rollup
            # written where the decision is computed (mcp_monthly_usage
            # endpoint → monthly_quota.record_wall_hit). A missing table
            # reads as zeros — accurate (no hit ever recorded), and the
            # dashboard shows an explicit 0 instead of a hole.
            try:
                from monthly_quota import wall_stats as _wall_stats
                out["quota_wall"] = _wall_stats(cur)
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out["quota_wall"] = None
                out["quota_wall_error"] = str(e)[:120]

            # ★★2026-07-30: + refunded_at IS NULL. This was the FOURTH conversion
            # surface and the last one still counting refunded sales as revenue.
            # canonical_funnel, funnel_health and /health were all filtered
            # (#1885, #1888) — this one was missed, so the Upgrade Funnel
            # dashboard published "Paid conversions (30d): 10" while the honest
            # count was 6 (10 raw → 8 after refunds → 6 after comp/seed).
            # Adding a filter to three of four lock-stepped surfaces does not fix
            # drift, it MOVES it. Any filter added to one belongs in all four.
            cur.execute(
                "SELECT COUNT(*) FROM mcp_conversions "
                " WHERE created_at >= NOW() - INTERVAL '30 days' "
                "   AND refunded_at IS NULL"
            )
            out["conversions_30d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT tier, COUNT(*) FROM mcp_dev_keys WHERE status='active' GROUP BY tier"
            )
            out["keys_by_tier"] = {r[0]: r[1] for r in cur.fetchall()}

            # Item F (2026-06-02): read from canonical mcp_funnel_real view
            # so the top-tool ranking excludes synthetic/probe traffic
            # (was previously polluted by dchub-selfheal hammering
            # get_grid_intelligence).
            # Item r-funnel-distinct (2026-06-05): rank by DISTINCT sessions/callers,
            # NOT raw COUNT(*). The generic 'mcp' client bucket (anonymous LLM-proxy,
            # NULL ip) fires thousands of signals from a handful of sessions, so raw
            # count over-stated a few power tools (market_intel/grid_data/water_risk
            # ~3-4k each) while masking real distinct demand. Mirrors the
            # paid_tool_demand_30d treatment (COUNT(DISTINCT ip_address)). `n` is
            # retained as de-emphasized context for any existing consumer.
            cur.execute(
                """SELECT tool_requested,
                          COUNT(*)                    AS n,
                          COUNT(DISTINCT session_id)  AS sessions,
                          COUNT(DISTINCT ip_address)  AS callers
                   FROM mcp_funnel_real
                   WHERE created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY tool_requested
                   ORDER BY sessions DESC, n DESC LIMIT 10"""
            )
            out["top_signal_tools_30d"] = [
                {"tool": r[0], "n": r[1], "sessions": r[2], "callers": r[3]}
                for r in cur.fetchall()
            ]

            # 2026-06-15: exclude OUR OWN monitoring/test traffic so the
            # "addressable demand pool" reflects real external agents. This query
            # reads mcp_tool_calls raw (no is_synthetic view), so dchub-selfheal +
            # *-test/probe inflated "distinct users" (~194→175 for grid). Reuse the
            # canonical prefix list (single source of truth) against client_name.
            try:
                from mcp_upgrade_gate import _SYNTHETIC_CLIENT_PREFIXES as _synp
            except Exception:
                _synp = ('dchub-', 'step2_', 'qa-', 'probe-', 'test-', 'monitor-',
                         'healthcheck', 'r51-', 'r52-', 'e2e-', 'recheck')
            _syn_clause = "".join(
                " AND LOWER(COALESCE(client_name,'')) NOT LIKE %s" for _ in _synp)
            _syn_params = tuple(str(p).lower() + "%" for p in _synp)
            cur.execute(
                """SELECT tool_name, COUNT(*) AS n,
                          COUNT(DISTINCT ip_address) AS users
                   FROM mcp_tool_calls
                   WHERE tool_name = ANY(%s)
                     AND created_at >= NOW() - INTERVAL '30 days'""" + _syn_clause + """
                   GROUP BY tool_name ORDER BY n DESC""",
                (["analyze_site", "compare_sites", "get_grid_intelligence",
                  "get_dchub_recommendation", "get_fiber_intel"], *_syn_params),
            )
            out["paid_tool_demand_30d"] = [
                {"tool": r[0], "calls": r[1], "users": r[2]} for r in cur.fetchall()
            ]

            # Phase JJ batch 3 (2026-05-14): per-platform funnel breakdown.
            # 8K upgrade signals × 0.05% conversion is the headline business
            # problem; we couldn't tell where the drop-off happened because
            # nobody was aggregating by mcp_client. Schema already had the
            # column (mcp_analytics_postgres.py:88); this exposes it.
            #
            # Each row tells you: per AI platform, how many tool calls,
            # how many upgrade signals (= they hit a paid tool), and how
            # many distinct users. Comparing platforms reveals which AI
            # agents convert humans best (Claude vs ChatGPT vs Cursor etc).
            try:
                # Item F (2026-06-02): migrate to mcp_funnel_real view; the
                # inline _signal_excl_clause is now provided by the view's
                # is_synthetic=FALSE filter. Date-range/tier filters kept.
                #
                # brain-l15 #1437/#1445 (2026-07-05): raw mcp_client sometimes IS
                # a browser User-Agent ('mozilla/5.0 (linux; android 10; k)…'),
                # which leaked through as a "platform" with sessions=0. Normalize
                # UA-shaped / oversized values into a single 'web-unattributed'
                # bucket so they stop fragmenting the platform list.
                # brain-l15 #1438/#1446 (2026-07-05): `converted` used to read the
                # mcp_upgrade_signals.converted FLAG, which is ~never set for the
                # anonymous MCP funnel — so every platform showed converted=0 while
                # conversions_30d=9. Attribute real conversions via
                # mcp_conversions.attribution_signal_id -> signal.id instead, so a
                # conversion counts for the platform of the signal that drove it.
                cur.execute(
                    """SELECT platform,
                              COUNT(DISTINCT sig_id)  AS signals,
                              COUNT(DISTINCT session_id) AS sessions,
                              COUNT(DISTINCT ip_address) AS unique_ips,
                              COUNT(DISTINCT conv_id) AS converted
                       FROM (
                           SELECT
                             s.id AS sig_id, s.session_id, s.ip_address,
                             c.id AS conv_id,
                             CASE
                               WHEN COALESCE(s.mcp_client,'') = '' THEN 'unknown'
                               WHEN LOWER(s.mcp_client) LIKE 'mozilla/%'
                                 OR LOWER(s.mcp_client) LIKE '%mozilla%'
                                 OR LOWER(s.mcp_client) LIKE '%(linux%'
                                 OR LOWER(s.mcp_client) LIKE '%(windows%'
                                 OR LOWER(s.mcp_client) LIKE '%(macintosh%'
                                 OR LOWER(s.mcp_client) LIKE '%(iphone%'
                                 OR LOWER(s.mcp_client) LIKE '%android%'
                                 OR LOWER(s.mcp_client) LIKE '%applewebkit%'
                                 OR LENGTH(s.mcp_client) > 48
                                 THEN 'web-unattributed'
                               ELSE LOWER(s.mcp_client)
                             END AS platform
                           FROM mcp_funnel_real s
                           LEFT JOIN mcp_conversions c
                             ON c.attribution_signal_id = s.id
                            AND c.created_at >= NOW() - INTERVAL '30 days'
                           WHERE s.created_at >= NOW() - INTERVAL '30 days'
                             -- brain-l15 #1600 followup (2026-07-14): exclude our own
                             -- probe/harness/test signals (clawith, value-harness,
                             -- funnel-diag, probe*, ...) so signals_by_platform shows
                             -- real agent demand, not self-instrumentation. Same
                             -- de-loop verdict as calls_by_platform; keeps anonymous
                             -- browser signals ('web-unattributed') + NULL mcp_client.
                             AND """ + _deloop_external_platform_predicate('s.mcp_client') + """
                       ) q
                       GROUP BY platform
                       ORDER BY signals DESC
                       LIMIT 20"""
                )
                out["signals_by_platform_30d"] = [
                    {
                        "platform": r[0],
                        "signals": r[1],
                        "sessions": r[2],
                        "unique_ips": r[3],
                        "converted": r[4] or 0,
                        "conv_rate_pct": round((r[4] or 0) / max(r[1], 1) * 100, 3),
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out["signals_by_platform_30d_error"] = str(e)[:120]

            # brain-l15 #1438/#1446 (2026-07-05): per-platform CONVERSION
            # attribution + an explicit unattributed bucket. Answers "are the 9
            # conversions a severed join, or genuinely unattributable?" — each
            # conversion is mapped to the platform of the signal it references
            # (attribution_signal_id first, then a caller_id match).
            # brain-l15 #1577 (2026-07-13): when there is NO signal link, fall back
            # to the conversion's OWN source instead of a blanket 'unattributed'.
            # The current conversions are all web:pricing-page / organic_no_mcp_touch
            # — genuinely NOT agent-driven, so they have no MCP signal to join and
            # calling them 'unattributed' overstated it as a severed join / lost data
            # when the source column already names the real channel. Now: web/organic
            # sales bucket as 'web-direct' / 'organic-direct' (a real attribution);
            # only a genuinely MCP-originated conversion whose signal link is broken
            # stays 'unattributed', so a REAL severance still surfaces.
            # SUM(attributed)+unattributed == conversions_30d.
            try:
                cur.execute(
                    """WITH conv AS (
                         SELECT c.id AS conv_id,
                                COALESCE((
                                  SELECT CASE
                                    WHEN COALESCE(s.mcp_client,'') = '' THEN 'unknown'
                                    WHEN LOWER(s.mcp_client) LIKE 'mozilla/%'
                                      OR LOWER(s.mcp_client) LIKE '%mozilla%'
                                      OR LENGTH(s.mcp_client) > 48
                                      THEN 'web-unattributed'
                                    ELSE LOWER(s.mcp_client)
                                  END
                                  FROM mcp_upgrade_signals s
                                  WHERE s.id = c.attribution_signal_id
                                     OR (COALESCE(c.caller_id,'') <> ''
                                         AND s.caller_id = c.caller_id)
                                  ORDER BY (s.id = c.attribution_signal_id) DESC NULLS LAST,
                                           s.created_at DESC
                                  LIMIT 1
                                ),
                                -- #1660 residual (r-keybound-platform 2026-07-18):
                                -- key-bound buys (pk-/k- refs) often have NO
                                -- signal to join — the webhook now stamps the
                                -- key's dominant call-history platform onto the
                                -- row itself (mcp_signal_canonical.
                                -- resolve_key_platform); read it before falling
                                -- back to the channel buckets so agent-driven
                                -- revenue buckets under its real platform.
                                NULLIF(LOWER(TRIM(c.platform)), ''),
                                CASE  -- no signal link: use the conversion's own channel
                                     WHEN c.source LIKE 'web:%'
                                          OR COALESCE(c.web_source,'') <> '' THEN 'web-direct'
                                     WHEN c.source LIKE 'organic%' THEN 'organic-direct'
                                     ELSE 'unattributed'
                                   END) AS platform
                         FROM mcp_conversions c
                         WHERE c.created_at >= NOW() - INTERVAL '30 days'
                       )
                       SELECT platform, COUNT(*) AS conversions
                       FROM conv GROUP BY platform ORDER BY conversions DESC"""
                )
                _cbp = [{"platform": r[0], "conversions": int(r[1] or 0)}
                        for r in cur.fetchall()]
                out["conversions_by_platform_30d"] = _cbp
                out["conversions_attributed_30d"] = sum(
                    x["conversions"] for x in _cbp if x["platform"] != "unattributed")
                out["conversions_unattributed_30d"] = sum(
                    x["conversions"] for x in _cbp if x["platform"] == "unattributed")

                # ★★★ r-attribution-truth (2026-08-24). THE WORD "ATTRIBUTED"
                # WAS DOING TWO JOBS.
                #
                # The bucketing above is right and #1577's reasoning still
                # holds: a web:pricing-page sale genuinely has no MCP signal to
                # join, and calling it 'unattributed' overstated a severed
                # join. But the two SCALARS derived from it inherited the word
                # "attributed" for a bucket assigned precisely BECAUSE no
                # signal link existed. The consequence is structural, not
                # cosmetic: 'unattributed' now requires a row with no signal,
                # no key-bound platform AND a source that is neither web nor
                # organic, so conversions_unattributed_30d is near-permanently
                # 0 — and a 0 in a field named "unattributed" reads as "no
                # attribution was lost", which is the opposite of what it
                # measures.
                #
                # Measured 2026-08-24: the dashboard tile published
                # "Paid conversions (30d): 6 — real paid customers via
                # web/organic handoff — the revenue KPI" with
                # conversions_attributed_30d=6 / unattributed=0, while
                # paid_signal_attribution_30d in the SAME payload read
                # paid_total=4, bridged_to_signal=1, attribution_rate=25.0%.
                # Nothing was wrong with either number. Nothing named the gap.
                #
                # Values of the two published scalars are UNCHANGED (public
                # endpoint, outside readers, and test_published_truth_shell_54
                # binds them). The split below is additive and says which half
                # of "attributed" a reader is holding. Derived from _cbp — no
                # second query, so it cannot disagree with the table above.
                _split = _split_conversion_attribution(_cbp)
                out["conversions_channel_fallback_30d"] = _split["channel_fallback"]
                out["conversions_signal_linked_30d"] = _split["signal_linked"]
                out["conversions_attribution_basis"] = (
                    "conversions_attributed_30d counts every row that received "
                    "ANY bucket other than 'unattributed' — INCLUDING the "
                    "'web-direct'/'organic-direct' channel fallback, which the "
                    "SQL assigns precisely when there is NO signal link and no "
                    "key-bound platform. It therefore means 'we know which "
                    "CHANNEL this sale came through', NOT 'this sale is traced "
                    "to an MCP signal'. Read the additive split instead: "
                    "conversions_signal_linked_30d = bucketed via a real "
                    "mcp_upgrade_signal or a key-bound platform; "
                    "conversions_channel_fallback_30d = no MCP linkage at all, "
                    "bucketed by the sale's own source column; "
                    "conversions_unattributed_30d = neither, which is "
                    "near-structurally 0 and must NOT be read as 'no "
                    "attribution was lost'. The three sum to conversions_30d. "
                    "★For end-to-end signal->paid measurability read "
                    "paid_signal_attribution_30d, which asks the stricter "
                    "question over a stricter population — see "
                    "conversions_reconciliation_30d for why its total differs."
                )
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out["conversions_by_platform_30d_error"] = str(e)[:120]

            # instrument-before-spend (2026-07-20): per-SIGNAL -> paid BRIDGE audit.
            # conversions_by_platform_30d above buckets paid rows by PLATFORM and
            # treats web/organic-sourced sales as "attributed" to a channel. This
            # field asks the harder, honest question the growth read needs before
            # any spend: of the *honest* paid conversions (same filter as
            # conversions_30d_real — stripe_customer_id present, seed/comp/NLR
            # excluded, i.e. the real "11 paid"), how many can be BRIDGED end-to-end
            # to an upstream mcp_upgrade_signal, and how many genuinely CANNOT?
            #   bridge = attribution_signal_id points at a real signal, OR a shared
            #            caller_id (email / session_id / anon-hash — the canonical
            #            handoff key) whose signal PRECEDED the sale.
            #   unattributable = neither exists. We report it HONESTLY rather than
            #            fabricating a link: agent-channel buys land here because the
            #            Stripe webhook has no MCP signal to join, which is exactly
            #            why the 3,591-signals -> 11-paid path is unmeasurable
            #            end-to-end today. No bound params / no LIKE (empty-tuple %
            #            trap); own try/except with rollback — additive, fail-open.
            try:
                cur.execute(
                    """WITH paid AS (
                         SELECT c.id AS conv_id,
                                c.created_at AS conv_at,
                                c.attribution_signal_id AS sig_id,
                                NULLIF(LOWER(TRIM(c.caller_id)), '') AS caller_id
                         FROM mcp_conversions c
                         WHERE c.created_at >= NOW() - INTERVAL '30 days'
                           AND c.stripe_customer_id IS NOT NULL
                           AND LOWER(COALESCE(c.plan_to,'')) NOT IN
                               ('comp','complimentary','research_seed_nlr','seed')
                           -- ★2026-08-01: free-text seed labels (the live NLR
                           -- rows say 'Year 1 Research Seed — FY2026
                           -- calibration'); POSITION keeps this %-free.
                           AND POSITION('seed' IN LOWER(COALESCE(c.plan_to,''))) = 0
                           AND LOWER(COALESCE(c.source,'')) <> 'seed'
                       ),
                       labeled AS (
                         SELECT p.conv_id,
                                CASE
                                  WHEN p.sig_id IS NOT NULL AND EXISTS (
                                         SELECT 1 FROM mcp_upgrade_signals s
                                         WHERE s.id = p.sig_id)
                                    THEN 'signal_id'
                                  WHEN p.caller_id IS NOT NULL AND EXISTS (
                                         SELECT 1 FROM mcp_upgrade_signals s
                                         WHERE NULLIF(LOWER(TRIM(s.caller_id)),'') = p.caller_id
                                           AND s.created_at <= p.conv_at)
                                    THEN 'caller_bridge'
                                  ELSE 'unattributable'
                                END AS bridge
                         FROM paid p
                       )
                       SELECT bridge, COUNT(*) FROM labeled GROUP BY bridge"""
                )
                _bridge = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}
                _sig = _bridge.get("signal_id", 0)
                _cal = _bridge.get("caller_bridge", 0)
                _un  = _bridge.get("unattributable", 0)
                _paid_total = _sig + _cal + _un
                out["paid_signal_attribution_30d"] = {
                    "paid_total":                          _paid_total,
                    "bridged_to_signal":                   _sig + _cal,
                    "bridged_via_attribution_signal_id":   _sig,
                    "bridged_via_caller_key":              _cal,
                    "unattributable":                      _un,
                    "attribution_rate_pct": (
                        round(100.0 * (_sig + _cal) / _paid_total, 1)
                        if _paid_total else None),
                    "definition": (
                        "honest paid = stripe_customer_id NOT NULL, seed/comp/NLR "
                        "excluded (identical filter to conversions_30d_real). "
                        "'bridged' = the paid row links to an upstream "
                        "mcp_upgrade_signal via attribution_signal_id OR a shared "
                        "caller_id (email/session/anon-hash) whose signal preceded "
                        "the sale. 'unattributable' = no such bridge exists — "
                        "reported honestly, NOT fabricated. This is the true "
                        "end-to-end measurability of the signal -> paid path."),
                }
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out["paid_signal_attribution_30d_error"] = str(e)[:120]

            # ★★★ r-attribution-truth (2026-08-24): ONE BLOCK A TILE CAN RENDER
            # WITHOUT LYING.
            #
            # This payload publishes THREE different true counts of "paid":
            #   conversions_30d                        — 30d, refunded_at IS NULL
            #   paid_signal_attribution_30d.paid_total — the above, PLUS
            #                                            stripe_customer_id NOT
            #                                            NULL and seed/comp/NLR
            #                                            excluded
            #   ...bridged_to_signal                   — of those, the ones
            #                                            traceable to a signal
            # Measured 2026-08-24 they read 6 / 4 / 1. Every one is correct.
            # A tile that renders the first and calls it "the revenue KPI" is
            # not, and that is what shipped: three numbers, three populations,
            # one headline, no stated relationship between them. Same class as
            # the 07-30 refund drift (four lock-stepped surfaces, one missed
            # filter) — except here the surfaces do not need aligning, they
            # need NAMING, because the differences are all deliberate.
            #
            # Computed, never hardcoded, and fail-open: if the bridge query
            # above errored, honest_paid is None and the block SAYS so rather
            # than silently falling back to the looser count — a tile reading
            # this must be able to tell "not measured" from "measured zero".
            try:
                _psa = out.get("paid_signal_attribution_30d") or {}
                _c30 = out.get("conversions_30d")
                _honest = _psa.get("paid_total")
                _bridged = _psa.get("bridged_to_signal")
                out["conversions_reconciliation_30d"] = {
                    "conversions_30d": _c30,
                    "honest_paid_30d": _honest,
                    "excluded_by_honest_filter": (
                        (_c30 - _honest)
                        if (_c30 is not None and _honest is not None) else None),
                    "signal_bridged_30d": _bridged,
                    "measured": _honest is not None,
                    "ladder": (
                        "conversions_30d >= honest_paid_30d >= "
                        "signal_bridged_30d — each step applies a STRICTER "
                        "filter to the same table, so a drop between them is "
                        "by design, not lost data."),
                    "note": (
                        "conversions_30d counts mcp_conversions rows in 30d "
                        "with refunded_at IS NULL. honest_paid_30d additionally "
                        "requires stripe_customer_id NOT NULL and excludes "
                        "seed/comp/NLR plans; the difference "
                        "(excluded_by_honest_filter) is those rows and is NOT "
                        "missing data. signal_bridged_30d is how many of the "
                        "honest paid can be traced end-to-end to an upstream "
                        "mcp_upgrade_signal. ★Quote honest_paid_30d as revenue "
                        "and signal_bridged_30d as MCP-attributable revenue. "
                        "conversions_30d is the loosest of the three and is "
                        "the one that must NOT be labelled 'the revenue KPI'."),
                }
            except Exception as e:
                out["conversions_reconciliation_30d"] = None
                out["conversions_reconciliation_30d_error"] = str(e)[:120]

            # ★★★ r-install-score (2026-08-24). THE DISTRIBUTION ARTIFACT
            # NOBODY WAS COUNTING.
            #
            # /install/{claude,chatgpt,grok,perplexity,cursor} shipped
            # 2026-08-19 (dchub-frontend#1215) and all five are live. Nothing
            # scored them. Asked on 2026-08-24 "is the distribution we already
            # built working?", the honest answer was "unknown" — five days
            # after shipping it, with a growth decision waiting on the answer.
            #
            # ★The score works WITHOUT the gateway change: `?via=` is still
            # inert server-side, but the pages claim keys as
            # client_name='install-<client>' and /api/v1/keys/claim stores
            # client_name in mcp_dev_keys.metadata. So score on distinct
            # api_key — NEVER sessions, NEVER IPs. Grok rotates egress IP per
            # request AND opens a session per tool call, inflating both ~10x.
            #
            # ★★ REGISTRATION IS NOT FUNCTION. A minted key that never calls is
            # not distribution, it is a row. Smithery's 160 keys are all free
            # tier and 6.9% returned on a second day. So this publishes a
            # LADDER — minted -> called -> returned — and the dashboard must
            # render the whole ladder, not the first rung.
            #
            # ★ UNDERCOUNTS BY DESIGN, and says so: /api/v1/keys/claim is
            # idempotent per (client_name, ip) but an IP already holding a
            # gated key gets that key back with its ORIGINAL client_name. An
            # install-page visitor who already had a key is therefore invisible
            # here. Read this as a FLOOR.
            #
            # No bound params anywhere below — the literal % in LIKE would
            # otherwise hit the psycopg2 empty-tuple percent trap.
            try:
                cur.execute(
                    """WITH ik AS (
                         SELECT api_key,
                                metadata->>'client_name' AS cn
                           FROM mcp_dev_keys
                          WHERE metadata->>'client_name' LIKE 'install-%'
                            AND created_at >= NOW() - INTERVAL '30 days'
                       ),
                       act AS (
                         SELECT l.api_key,
                                COUNT(*) AS calls,
                                COUNT(DISTINCT date_trunc('day', l.timestamp))
                                  AS active_days
                           FROM mcp_call_log l
                          WHERE l.api_key IN (SELECT api_key FROM ik)
                          GROUP BY l.api_key
                       )
                       SELECT ik.cn,
                              COUNT(*) AS keys_minted,
                              COUNT(*) FILTER (
                                WHERE COALESCE(act.calls, 0) > 0)
                                AS keys_that_called,
                              COUNT(*) FILTER (
                                WHERE COALESCE(act.active_days, 0) >= 2)
                                AS keys_returned,
                              COALESCE(SUM(act.calls), 0) AS calls
                         FROM ik LEFT JOIN act ON act.api_key = ik.api_key
                        GROUP BY ik.cn
                        ORDER BY keys_minted DESC, ik.cn"""
                )
                _by_client = [
                    {"client": (r[0] or "").replace("install-", "") or "unknown",
                     "keys_minted": int(r[1] or 0),
                     "keys_that_called": int(r[2] or 0),
                     "keys_returned": int(r[3] or 0),
                     "calls": int(r[4] or 0)}
                    for r in (cur.fetchall() or [])
                ]
                _tot = {k: sum(x[k] for x in _by_client) for k in
                        ("keys_minted", "keys_that_called", "keys_returned",
                         "calls")}
                out["install_artifact_30d"] = {
                    "measured": True,
                    "keys_minted": _tot["keys_minted"],
                    "keys_that_called": _tot["keys_that_called"],
                    "keys_returned": _tot["keys_returned"],
                    "calls": _tot["calls"],
                    "by_client": _by_client,
                    "pages": ["claude", "chatgpt", "grok", "perplexity",
                              "cursor"],
                    "ladder": (
                        "keys_minted >= keys_that_called >= keys_returned. "
                        "MINTED IS NOT DISTRIBUTION — a key that never called "
                        "is a row, not a user. keys_returned (active on 2+ "
                        "distinct days) is the only rung that means the "
                        "channel delivered someone who came back."),
                    "basis": (
                        "distinct mcp_dev_keys.api_key whose "
                        "metadata->>'client_name' matches 'install-%' and was "
                        "created in the last 30 days, LEFT JOINed to "
                        "mcp_call_log on api_key (that table's time column is "
                        "`timestamp`, not created_at). Scored on KEYS, never "
                        "sessions or IPs — Grok rotates egress IP per request "
                        "and opens a session per tool call, inflating both by "
                        "~10x. ★This is a FLOOR: /api/v1/keys/claim returns an "
                        "EXISTING key (with its original client_name) to an IP "
                        "that already holds one, so an install-page visitor "
                        "who already had a key never appears here. A zero "
                        "means 'no NEW keys traced to the pages', not 'nobody "
                        "visited'."),
                }
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out["install_artifact_30d"] = {"measured": False,
                                               "error": str(e)[:120]}

            # Per-platform tool-call totals — pairs with signals_by_platform
            # so we can compute "signal rate" (% of calls that hit a paywall)
            # per platform. Shows whether some platforms are pinging paid
            # tools more aggressively than others.
            try:
                # NOTE: mcp_tool_calls has BOTH a `client_name` and a
                # `platform` column. Aliasing the COALESCE expression
                # `AS platform` and then `GROUP BY platform` made Postgres
                # bind to the real `platform` column, not the alias —
                # leaving client_name ungrouped → "must appear in GROUP
                # BY" error. Fix: alias as `client_platform` (no column
                # collision) and GROUP BY the full expression.
                #
                # Phase ZZZZ-attribution (2026-05-18): client_name is
                # null/empty for 23K+ calls (most agents don't send
                # clientInfo.name in initialize handshake). Backfill via
                # user_agent pattern-matching so we actually know WHO is
                # hitting the funnel — was 'unknown' for 70%+ of traffic.
                # Phase ZZZZ-attr-v2: client_name is being set to session
                # UUIDs (not platform names) for most MCP traffic — those
                # 8-4-4-4-12 hex UUIDs pollute the platform list. Filter
                # them out via a regex check so we fall through to
                # user_agent classification.
                # Phase ZZZZZ-round9 (2026-05-23): tighten the classifier
                # so 90,000+ tool calls don't lump into 'node-script' +
                # 'unknown' (which is what mcp/funnel showed before this
                # commit — 50k + 40k = 98% of traffic unattributable).
                # Two layers added BEFORE the generic node/python buckets:
                #   1. Internal traffic — our own DCHub-* UAs (brain-radar,
                #      healer, sentinel, scheduler, smoke-test) sorted into
                #      'internal-dchub' so they don't pollute external
                #      conversion metrics.
                #   2. MCP SDK identification — @modelcontextprotocol/sdk,
                #      mcp-inspector, and the n8n MCP node all expose
                #      identifiable UA fragments. Catching them before the
                #      generic 'node-script' falls through means we know
                #      "this is an MCP agent" even when the host AI client
                #      didn't pass clientInfo.name.
                # 2026-06-19: single-sourced from mcp_calls_deloop.PLATFORM_CASE
                # so the GROUP-BY classifier here and the tool_calls_7d_real
                # FILTER below classify EVERY row identically — and identically
                # to routes/funnel_health. (Was an inline copy of the same CASE.)
                # brain-l15 #1600 (2026-07-14): this breakdown had NO probe filter,
                # so internal self-traffic surfaced as demand (dchub-selfheal was
                # #2, plus value-harness / clawith / probe / gating-audit — ~47% of
                # 30d volume). Apply the SAME real_calls_predicate() the honest
                # tool_calls_7d_real below FILTERs on, so the per-platform breakdown
                # is the de-looped external count broken out, not raw traffic. The
                # narrow _PROBE_PLATFORMS IN-list alone missed the dchub-%/harness/
                # test/audit families; the shared predicate carries those prefix
                # rules (mcp_calls_deloop.external_platform_predicate + real UA).
                _platform_case = _DELOOP_PLATFORM_CASE
                cur.execute(
                    f"""SELECT
                          {_platform_case} AS client_platform,
                          COUNT(*) AS calls,
                          COUNT(DISTINCT ip_address) AS unique_ips
                       FROM mcp_tool_calls
                       WHERE created_at >= NOW() - INTERVAL '30 days'
                         AND {_deloop_real_calls_predicate()}
                       GROUP BY {_platform_case}
                       ORDER BY calls DESC
                       LIMIT 20"""
                )
                # r-platform-kind (2026-08-27): carry `kind` alongside the
                # count. This breakdown is PUBLIC -- this route is the
                # dashboard's aggregate stats and carries NO admin gate
                # (verified keyless, cf-cache-status: DYNAMIC). 93.2% of its
                # 30d volume is NOT demand — a registry crawl at 37.6% from ONE
                # ip, an unidentifiable generic client at 36.7%, and a bulk
                # harvester at 18.9% from TWO ips — all presented in the same
                # shape and sort as the ~4% that is real assistant traffic.
                # A caller reading calls DESC reads a harvester as the #3
                # platform. `kind` is what makes that legible without moving a
                # single count: the numbers are unchanged, only labelled.
                # ★#3247 also claimed routes/health_json.py republishes these
                # rows on /data/growth.json. It does not, and #3248 corrected
                # that: dchub.cloud/data/growth.json is a static CF Pages asset
                # with a different schema. The Flask route sharing the path was
                # origin-only and was DELETED 2026-08-28 along with the rest of
                # its blueprint, all seven routes of which were shadowed the
                # same way. THIS route is the only surface for the breakdown,
                # which is why the label has to be right here.
                # ★ Kinds come from routes/platform_attribution.classify_platform
                # and nowhere else. classify_deloop_platform bridges THIS query's
                # PLATFORM_CASE name-space to that function's canonical one;
                # unbridged it returned 'unknown' for 79.3% of live volume.
                # Imported lazily and defensively — a classifier problem must
                # degrade to an unlabelled row, never take out the funnel.
                try:
                    from routes.platform_attribution import (
                        classify_deloop_platform as _kind,
                    )
                except Exception:  # noqa: BLE001
                    _kind = lambda _p: None  # noqa: E731
                out["calls_by_platform_30d"] = [
                    {"platform": r[0], "calls": r[1], "unique_ips": r[2],
                     "kind": _kind(r[0])}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["calls_by_platform_30d_error"] = str(e)[:120]

            # Phase FF+25-followup-r3 (2026-05-20): probe-filtered counts.
            # Same _platform_case classifier as above, but COUNTed at 7d
            # window with the probe platforms excluded. These are what the
            # public /cited-by + homepage dashboards should display.
            try:
                # No bound params here: passing %s tripped the driver two ways
                # (psycopg2 parsed the LIKE '%chatgpt%' in _platform_case as
                # placeholders → "got '%c'"; and the tuple binding rendered a
                # bare "$1" Postgres couldn't parse). The probe list is a
                # trusted hardcoded constant, so inline it as a SQL literal
                # IN-list — no binding, so _platform_case's % are left alone.
                #
                # 2026-06-19: the real/probe predicate is built by the shared
                # mcp_calls_deloop.real_calls_predicate() — the SAME function
                # routes/funnel_health._deloop_calls_where() returns — so this
                # endpoint's tool_calls_7d_real is byte-identical to the
                # dashboard's. (Was an inline `{_platform_case} NOT IN (...)`.)
                _real_pred  = _deloop_real_calls_predicate()      # TRUE = external
                _probe_pred = f"NOT {_real_pred}"
                cur.execute(
                    f"""SELECT
                          COUNT(*) FILTER (
                            WHERE {_real_pred}
                          ) AS real_calls,
                          COUNT(*) FILTER (
                            WHERE {_probe_pred}
                          ) AS probe_calls,
                          COUNT(DISTINCT ip_address) FILTER (
                            WHERE {_real_pred}
                          ) AS real_unique_ips
                       FROM mcp_tool_calls
                       WHERE created_at >= NOW() - INTERVAL '7 days'"""
                )
                _r = cur.fetchone() or (0, 0, 0)
                out["tool_calls_7d_real"]   = int(_r[0] or 0)
                out["tool_calls_7d_probes"] = int(_r[1] or 0)
                out["unique_ips_7d_real"]   = int(_r[2] or 0)
                # r-agent-parity (2026-07-31): unique_ips_7d_real is a
                # SECONDARY signal — COUNT(DISTINCT raw ip_address strings)
                # over-counts identity (some rows store whole XFF chains, so
                # one caller counts once per chain form) and applies the live
                # predicate only, without the identity view's internal-IP and
                # scripted-UA backstops. It published 129 while the canonical
                # count read 95 for the same window. Kept for back-compat;
                # never render it as "agents" — that's what
                # real_external_agents_7d below is for.
                out["unique_ips_7d_real_basis"] = (
                    "SECONDARY: DISTINCT raw ip_address strings (XFF chains "
                    "count once per form), live real_calls_predicate() only. "
                    "Superseded by real_external_agents_7d for any 'agents' "
                    "display.")
                # Convenience: list which platforms were classified as probes
                # so the UI can render a tooltip ("filtered: node-script,
                # python-script, curl, ...").
                out["probe_platforms"] = list(_PROBE_PLATFORMS)
            except Exception as e:
                out["tool_calls_7d_real_error"] = str(e)[:120]

            # r-agent-parity (2026-07-31): THE canonical agent count — the
            # same query the /ai tool-use widget (main._real_tool_use_7d) and
            # the growth memo run, single-sourced from mcp_calls_deloop so the
            # three public surfaces cannot publish three different "agents
            # (7d)" numbers again (shell #44 lane 3). Dashboards should render
            # THIS pair, labeled with the window + exclusions inline.
            try:
                cur.execute(_canonical_activity_sql(7))
                _car = cur.fetchone() or (0, 0)
                out["real_external_agents_7d"] = int(_car[0] or 0)
                out["real_external_calls_7d"] = int(_car[1] or 0)
                out["real_external_agents_basis"] = _CANONICAL_AGENTS_BASIS
                # r-basis-parity (2026-08-03): the PRIOR window on THIS basis.
                # Without it the dashboard had no canonical-basis trend, so it
                # borrowed tool_calls_wow_pct (+323.7%) — computed on
                # mcp_tool_calls / complete-days / a different predicate — and
                # printed it beside an agent count from this query (+20.6%).
                # Two populations, two windows, one card: the numbers could
                # not be reconciled by any reader, and the card looked broken
                # every afternoon because this window includes today while the
                # other stops at midnight. Now both halves of the pair, and
                # their trend, come from one query.
                cur.execute(_canonical_activity_sql(7, 7))
                _cap = cur.fetchone() or (0, 0)
                _pa, _pc = int(_cap[0] or 0), int(_cap[1] or 0)
                out["real_external_agents_prior_7d"] = _pa
                out["real_external_calls_prior_7d"] = _pc
                out["real_external_agents_wow_pct"] = (
                    round(100.0 * (out["real_external_agents_7d"] - _pa) / _pa, 1)
                    if _pa else None)
                out["real_external_calls_wow_pct"] = (
                    round(100.0 * (out["real_external_calls_7d"] - _pc) / _pc, 1)
                    if _pc else None)
                # ★ 2026-08-20 — THE ROLLING PAIR STRADDLES A CORRECTION TOO.
                # weekly-series learned to refuse a delta across a definition
                # change; these flat keys never asked. The rolling window ending
                # now CONTAINS #202 (2026-08-18 06:31Z) while the prior window
                # is wholly before it, so this pair compares a corrected
                # population against the one the correction removed.
                _mark_wow_comparability(
                    out, _rolling_spans(7, 2),
                    ("real_external_agents_wow_pct",
                     "real_external_calls_wow_pct"),
                    "real_external_rolling_wow")

                # ★ 2026-08-08 — THE HEADLINE-SAFE TREND. The rolling pair
                # above is honest arithmetic and a misleading headline: a
                # distinct-AGENT count over a rolling window is dominated by
                # whichever outlier days sit inside it. Live proof the day this
                # shipped: rolling said **-65.3%** while the same population on
                # COMPLETE ISO weeks said **62 -> 85 = +37%** — the prior
                # rolling window happened to contain 2026-07-28 (30 agents in
                # one day, ~3x its neighbours) and the current one did not.
                # Nothing about the business changed between those two numbers.
                #
                # Complete weeks exclude the partial current week by
                # construction, so this can never compare a part-week against a
                # whole one. Rolling fields are KEPT (they answer "what is
                # happening right now"); the dashboard leads with this one.
                try:
                    from mcp_calls_deloop import (
                        canonical_external_complete_week_sql as _cw_sql)
                    cur.execute(_cw_sql(0))
                    _cw = cur.fetchone() or (0, 0)
                    cur.execute(_cw_sql(1))
                    _pw = cur.fetchone() or (0, 0)
                    _cwa, _cwc = int(_cw[0] or 0), int(_cw[1] or 0)
                    _pwa, _pwc = int(_pw[0] or 0), int(_pw[1] or 0)
                    out["real_external_agents_complete_wk"] = _cwa
                    out["real_external_agents_prior_complete_wk"] = _pwa
                    out["real_external_calls_complete_wk"] = _cwc
                    out["real_external_agents_complete_wk_wow_pct"] = (
                        round(100.0 * (_cwa - _pwa) / _pwa, 1) if _pwa else None)
                    out["real_external_calls_complete_wk_wow_pct"] = (
                        round(100.0 * (_cwc - _pwc) / _pwc, 1) if _pwc else None)
                    out["real_external_complete_wk_basis"] = (
                        "COMPLETE ISO weeks (Mon-Sun) on mcp_calls_identity "
                        "WHERE is_public_ip AND is_real_external — the partial "
                        "current week is excluded by construction. Prefer this "
                        "over the rolling WoW for any trend claim: a rolling "
                        "distinct-agent count is dominated by whichever outlier "
                        "days fall inside each window (2026-08-08: rolling read "
                        "-65.3% while complete weeks read +37% on the same "
                        "population, because one 30-agent day left the window). "
                        "★ 2026-08-20: 'prefer this for any trend claim' is "
                        "conditional on comparability, and this sentence "
                        "without that condition is what got the +89.5% across "
                        "#202 rendered as 'the trend number'. Complete weeks "
                        "fix the WINDOW, not the POPULATION — read "
                        "real_external_complete_wk_comparability.quotable_as_"
                        "trend before quoting either _wow_pct; when it is "
                        "false the pct is published as null and the reason "
                        "names itself.")
                    # ★ Attach the same comparability weekly-series publishes.
                    # The complete-week pair is the two ISO weeks before the
                    # in-progress one — the same weeks canonical_external_
                    # complete_week_sql(0) and (1) aggregate.
                    from datetime import date as _dt_date, timedelta as _dt_td
                    _today = _dt_date.today()
                    _mon = _today - _dt_td(days=_today.weekday())
                    _mark_wow_comparability(
                        out,
                        _week_spans([_mon - _dt_td(weeks=2),
                                     _mon - _dt_td(weeks=1)]),
                        ("real_external_agents_complete_wk_wow_pct",
                         "real_external_calls_complete_wk_wow_pct"),
                        "real_external_complete_wk")
                except Exception as _cwe:  # noqa: BLE001
                    out["real_external_complete_wk_error"] = str(_cwe)[:120]
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["real_external_agents_7d"] = None
                out["real_external_calls_7d"] = None
                out["real_external_agents_7d_error"] = str(e)[:120]

            # ★ 2026-08-05: build the press headline HERE, not at the
            # acceleration-fields block above, because it binds the canonical
            # pair computed immediately above. Runs on both paths of that
            # try/except — on the failure path the canonical fields are None
            # and the helper degrades to the lifetime sentence by design.
            _build_press_headline(out)

            # Time-to-conversion median per platform (days from first
            # upgrade signal to converted=true). Reveals whether some
            # platforms convert fast vs slow.
            try:
                # Item F (2026-06-02): time-to-convert reads from
                # mcp_funnel_real so synthetic probes don't skew the
                # median (probes never convert -> would otherwise inflate
                # "median days to convert" for the platforms they share
                # a name with).
                cur.execute(
                    """WITH per_session AS (
                         SELECT session_id,
                                COALESCE(NULLIF(LOWER(mcp_client), ''), 'unknown') AS platform,
                                MIN(created_at) AS first_signal,
                                MIN(converted_at) FILTER (WHERE converted = TRUE) AS conv_at
                         FROM mcp_funnel_real
                         WHERE created_at >= NOW() - INTERVAL '90 days'
                           -- brain-l15 #1600 followup (2026-07-14): drop self/probe
                           -- signals (value-harness et al.) from the convert-timing too
                           AND """ + _deloop_external_platform_predicate('mcp_client') + """
                         GROUP BY session_id, platform
                       )
                       SELECT platform,
                              COUNT(*) FILTER (WHERE conv_at IS NOT NULL) AS converted_sessions,
                              PERCENTILE_CONT(0.5) WITHIN GROUP (
                                ORDER BY EXTRACT(EPOCH FROM (conv_at - first_signal))/86400.0
                              ) FILTER (WHERE conv_at IS NOT NULL) AS median_days_to_convert
                       FROM per_session
                       GROUP BY platform
                       ORDER BY converted_sessions DESC"""
                )
                out["time_to_convert_90d"] = [
                    {
                        "platform": r[0],
                        "converted_sessions": r[1] or 0,
                        "median_days_to_convert": round(float(r[2]), 2) if r[2] is not None else None,
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["time_to_convert_90d_error"] = str(e)[:120]
    except Exception as e:
        out["error"] = str(e)
    # Item F (2026-06-02): readers migrated to mcp_funnel_real (canonical
    # is_synthetic=FALSE view). Flag flips to True so the dashboard /
    # brain-radar can tell which release of the funnel is live.
    out["canonical_view"] = "mcp_funnel_real"
    out["canonical_view_active"] = True
    return jsonify(out), 200


# ── GET /api/v1/mcp/timeseries — Hourly traffic series for the dashboard ──
#
# ── GET /api/ai-analytics — thin adapter for connect.html dashboard ────────
#
# r33-Q (2026-05-21) — /connect's live dashboard fetches /api/ai-analytics
# to populate three counters (total_requests, active_platforms,
# mcp_connections). The endpoint never existed; every visitor's poll
# returned 404 and the dashboard silently stayed at zeros. Brain found
# this via frontend-health probe logs.
#
# Implementation: aggregate from mcp_tool_calls (30d) — same source
# /api/v1/mcp/funnel uses but flattened into the three counters
# connect.html expects. Cached server-side for 60s to avoid hammering
# the DB on every poll (page polls every 60s anyway).

@mcp_bp.get("/api/ai-analytics")
def ai_analytics():
    """Live counters for the /connect page dashboard.

    Returns:
        success: bool
        total_requests: int       — all MCP tool calls in last 30d
        active_platforms: int     — distinct AI clients seen in last 30d
        mcp_connections: int      — active MCP dev keys
    """
    out = {"success": True, "total_requests": 0,
           "active_platforms": 0, "mcp_connections": 0}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["total_requests"] = int(cur.fetchone()[0] or 0)

            # Distinct real external AI clients in the last 30d. FIX 2026-07-03:
            # this counted DISTINCT platform FROM mcp_upgrade_signals — a column
            # that table does NOT have (platform lives there as `mcp_client`), so
            # it threw, the except swallowed it, and active_platforms was pinned
            # to 0 forever despite hundreds of connections. Count from the real
            # traffic table (mcp_tool_calls, same source as total_requests),
            # excluding internal/probe/unattributed tags so the number is honest.
            try:
                # qa-0704: the 07-03 blocklist could never converge — platform is
                # an OPEN vocabulary of raw client names (202 distinct in 30d:
                # QA-harness tags, truncated fragments like 'v'/'p'/'clawith',
                # one-off agent names), and 179 of them passed the filters, which
                # kept failing the growth shell's 1..30 sanity gate (measurement
                # lever stuck at 0.4). Flip to an ALLOWLIST: classify each raw
                # value against MCP_PLATFORM_MAP (the attribution root) plus the
                # known agent-client brands, and count distinct CANONICAL
                # platforms. Short map keys ('hf', 'poe', 'xai') match exact-only
                # so junk can't substring its way in.
                cur.execute(
                    "SELECT DISTINCT lower(platform) FROM mcp_tool_calls "
                    "WHERE created_at >= NOW() - INTERVAL '30 days' "
                    "AND platform IS NOT NULL AND platform <> '' "
                    "AND char_length(platform) <= 64"
                )
                _raws = [r[0] for r in (cur.fetchall() or []) if r and r[0]]
                _pmap = {}
                try:
                    from main import MCP_PLATFORM_MAP as _pmap  # lazy: main imports us first
                except Exception:
                    _pmap = {'claude': 'Claude', 'chatgpt': 'ChatGPT', 'openai': 'ChatGPT',
                             'gemini': 'Gemini', 'perplexity': 'Perplexity', 'cursor': 'Cursor',
                             'copilot': 'Copilot', 'cline': 'Cline', 'windsurf': 'Windsurf'}
                # Real MCP agent clients that aren't consumer AI brands but are
                # genuine external platforms reaching the server.
                _extra = {'smithery': 'Smithery', 'glama': 'Glama', 'opencode': 'opencode',
                          'devin': 'Devin', 'agent-tools': 'agent-tools.cloud',
                          'lobehub': 'LobeHub', 'continue': 'Continue'}
                _canon = set()
                for _raw in _raws:
                    for _k, _v in list(_pmap.items()) + list(_extra.items()):
                        if _raw == _k or (len(_k) >= 4 and _k in _raw):
                            _canon.add(_v)
                            break
                out["active_platforms"] = len(_canon)
            except Exception:
                conn.rollback()

            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_dev_keys WHERE status='active'"
                )
                out["mcp_connections"] = int(cur.fetchone()[0] or 0)
            except Exception:
                conn.rollback()
    except Exception as e:
        out["success"] = False
        out["error"] = str(e)[:200]
    return jsonify(out)


# Phase JJ (2026-05-13): the existing /funnel returns rolling 7d/30d
# aggregates which wobble ±N every refresh thanks to rolling-window
# math, making it impossible to tell at a glance whether MCP traffic
# is actually growing or declining. This endpoint returns proper
# hourly buckets so the dashboard can show a trend line.

@mcp_bp.get("/api/v1/mcp/timeseries")
def mcp_timeseries():
    """Hourly MCP traffic + gate-fire series for the last N hours.

    Query params:
      hours          int, default 168 (7d), max 720 (30d)
      bucket         'hour' (default) | 'day'

    Response shape:
      {
        "bucket": "hour" | "day",
        "from_iso": "2026-05-06T00:00:00Z",
        "to_iso":   "2026-05-13T00:00:00Z",
        "series": [
          {"ts": "...", "tool_calls": N, "upgrade_signals": M, "gate_fires": M},
          ...
        ],
        "totals":  {"tool_calls": ..., "upgrade_signals": ..., "conversion_rate_pct": ...}
      }

    Public-readable like /funnel — no admin key required. Aggregates only,
    no PII. Heavily indexed by (created_at) so the query is fast.
    """
    try:
        hours = max(1, min(int(request.args.get("hours", 168)), 720))
    except (TypeError, ValueError):
        hours = 168
    bucket = (request.args.get("bucket") or "hour").lower()
    if bucket not in ("hour", "day"):
        bucket = "hour"

    out: dict = {"bucket": bucket, "hours": hours}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Window bounds — return as ISO so frontends can use Date.parse.
            cur.execute("SELECT NOW() - (%s || ' hours')::INTERVAL, NOW()", (hours,))
            from_ts, to_ts = cur.fetchone()
            out["from_iso"] = from_ts.isoformat()
            out["to_iso"] = to_ts.isoformat()

            # Bucket function — date_trunc gives us aligned bins regardless
            # of when the call landed within the hour.
            trunc = f"date_trunc('{bucket}', created_at)"

            # Tool calls per bucket
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_tool_calls
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            calls_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Upgrade signals per bucket (= gate fires that emitted an
            # upgrade prompt). This is the key conversion-funnel input.
            # Item F (2026-06-02): timeseries reads from mcp_funnel_real
            # so the hourly chart doesn't spike on probe loops.
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_funnel_real
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            signals_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Conversions per bucket (paying customers; 30d-style)
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_conversions
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            conv_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Merge into a single ordered series. Use the union of bin
            # keys so a quiet hour still shows up as a zero row (avoids
            # confusing "gaps" in the chart).
            all_bins = sorted(set(calls_by_bin) | set(signals_by_bin) | set(conv_by_bin))
            series = [
                {
                    "ts": b,
                    "tool_calls":      calls_by_bin.get(b, 0),
                    "upgrade_signals": signals_by_bin.get(b, 0),
                    "conversions":     conv_by_bin.get(b, 0),
                }
                for b in all_bins
            ]
            out["series"] = series

            # Totals + the conversion rate the dashboard actually cares
            # about — signals are the right denominator, not raw calls,
            # because un-gated free-tier calls can't possibly convert.
            total_calls = sum(calls_by_bin.values())
            total_signals = sum(signals_by_bin.values())
            total_conv = sum(conv_by_bin.values())
            out["totals"] = {
                "tool_calls":      total_calls,
                "upgrade_signals": total_signals,
                "conversions":     total_conv,
                "conversion_rate_pct": (
                    round((total_conv / total_signals) * 100.0, 3)
                    if total_signals > 0 else None
                ),
            }
    except Exception as e:
        out["error"] = str(e)
        return jsonify(out), 500
    return jsonify(out), 200


# ── GET /api/v1/mcp/dashboard — Serve the dashboard HTML through Flask ────

@mcp_bp.get("/api/v1/mcp/dashboard")
def mcp_dashboard():
    """Serves static/mcp-dashboard.html via the /api/* path so Cloudflare proxies it."""
    try:
        with open("static/mcp-dashboard.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("dashboard not found", status=404)



# ── POST /api/v1/stripe/webhook-mcp — Stripe → mcp_conversions ─────────────

@mcp_bp.post("/api/v1/stripe/webhook-mcp")
def stripe_webhook_mcp():
    """Handle Stripe customer.subscription.{created,updated}.
    Records conversion in mcp_conversions with attribution to the most recent
    mcp_upgrade_signal for the customer's email.

    Configure on Stripe dashboard: Webhooks → Add endpoint:
      URL:     https://dchub.cloud/api/v1/stripe/webhook-mcp
      Events:  customer.subscription.created, customer.subscription.updated
      Secret:  store as STRIPE_WEBHOOK_SECRET_MCP env var on Railway.
               Also needs STRIPE_SECRET_KEY env var to look up customer email.
    """
    try:
        import stripe
    except ImportError:
        return jsonify({"error": "stripe library not installed"}), 500

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP") or os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            # No secret configured — accept (dev/test mode); production should set secret
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": "invalid signature", "detail": str(e)}), 400

    event_type = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")

    # r-onboarding-fix (2026-07-03): idempotency gate (defect #4). Share the same
    # dedupe ledger as the primary handler so retries / concurrent deliveries of
    # subscription.created|updated can't re-provision (dup dch_live_ key + dup
    # "paid" welcome email). Lazy import avoids a circular import at module load;
    # fail-OPEN so a gate error never drops a real event.
    _evt_id = event.get("id", "") if isinstance(event, dict) else getattr(event, "id", "")
    try:
        from main import _stripe_event_already_processed as _seen_evt
        # r-idempotency-namespace (2026-07-13): /stripe/webhook and this /stripe/webhook-mcp
        # endpoint both receive the SAME Stripe event.id and share ONE event_id-keyed
        # ledger — whichever recorded it first STARVED the other ('idempotent_skip'),
        # so a pack credit-grant/key-mint (main only) OR a subscription conversion+
        # attribution record (this handler only) was silently dropped. Namespace THIS
        # endpoint's dedupe key so the two ledgers are disjoint: both handlers now run
        # for the same event, while same-endpoint retries still dedupe correctly.
        if _evt_id and _seen_evt(f"mcp::{_evt_id}", event_type):
            print(f"↩️ [mcp webhook] event {_evt_id} ({event_type}) already processed — idempotent skip")
            return jsonify({"received": True, "idempotent_skip": True}), 200
    except Exception as _ge:
        print(f"⚠️ [mcp webhook] idempotency gate error (fail-open): {str(_ge)[:120]}")

    # r89-conv (2026-06-14): checkout.session.completed is the ONLY Stripe event
    # that carries client_reference_id = the MCP session_id we bind onto every
    # Stripe URL (_stripeWithSession in the MCP server). The subscription.* events
    # below DON'T carry it, so session-based attribution never ran → the funnel's
    # signal→conversion link was blind for the ~99% of agent signals with NULL
    # email. Handle it here: flip the matching signal converted=TRUE + backfill
    # its email by session_id (mark_signals_converted Path 2) — which ALSO lets
    # the subscription.created handler below then attribute by the now-backfilled
    # email regardless of event order — and best-effort link an existing
    # conversion row. Additive early-return: it does NOT insert a conversion
    # (subscription.created does), so there is no double-count.
    # ★★2026-07-28: REFUNDS WERE NEVER REVERSED. mcp_conversions only ever grew;
    # no Stripe refund event was handled at all (the webhook covered
    # checkout.session.completed + customer.subscription.* + invoice.payment_failed
    # and nothing else). A refunded sale therefore stayed in the ledger as revenue
    # forever.
    #
    # This was not hypothetical: gabriel.zuckerman@nlr.gov was double-billed
    # $3,000/yr across two customer records, the duplicate was refunded by hand in
    # Stripe — and the ledger never learned. Two $3,000 rows then carried 77% of
    # May and 96% of June's reported MRR, manufacturing an apparent 84% "collapse"
    # into July that never happened. Every MRR figure read off this table was
    # overstated by refunded revenue.
    #
    # Stamp the conversion instead of deleting it: a refund is a real event in the
    # customer's history and the row is the audit trail. Reads exclude
    # `refunded_at IS NOT NULL` (see canonical_funnel.py) rather than losing it.
    # ★Match on stripe_customer_id — mcp_conversions carries no charge/invoice id.
    if event_type in ("charge.refunded", "charge.refund.updated"):
        try:
            _obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
            _obj = dict(_obj) if not isinstance(_obj, dict) else _obj
            # charge.refund.updated delivers a Refund; charge.refunded a Charge.
            _cust = _obj.get("customer")
            _refunded_cents = int(_obj.get("amount_refunded") or _obj.get("amount") or 0)
            # ★★★NET THE REFUND OFF ANY KEPT CHARGE OF THE SAME AMOUNT before
            # stamping. A customer's charge list contains their RECURRING monthly
            # charges, not just the opening sale: cbraun@cbecommercial.com has
            # SIX $99 charges on one customer — 4 kept, 1 refunded, 1 failed —
            # i.e. an active monthly subscriber who had ONE month refunded.
            # Stamping on the refund event alone would mark a live recurring
            # customer as refunded and delete him from MRR, the mirror image of
            # the bug this fixes. Only stamp when refunded charges of this amount
            # OUTNUMBER kept ones. ★`status == 'succeeded'` is required for
            # "kept" — a FAILED charge is not a payment and must not offset.
            _net_refunded = True
            try:
                if _cust and _refunded_cents > 0:
                    import stripe as _st_r
                    _st_r.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
                    _chs = _st_r.Charge.list(customer=_cust, limit=100)
                    _ref_n = sum(1 for _c2 in _chs.get("data", [])
                                 if int(_c2.get("amount_refunded") or 0) == _refunded_cents)
                    _kept_n = sum(1 for _c2 in _chs.get("data", [])
                                  if int(_c2.get("amount_refunded") or 0) == 0
                                  and _c2.get("status") == "succeeded"
                                  and int(_c2.get("amount") or 0) == _refunded_cents)
                    _net_refunded = _ref_n > _kept_n
                    if not _net_refunded:
                        print(f"↩️ [mcp webhook] refund for {_cust} "
                              f"${_refunded_cents/100:.2f} is OFFSET by {_kept_n} kept "
                              f"charge(s) of the same amount — customer still paying, "
                              f"NOT stamping")
            except Exception as _ne:
                # Cannot verify → do NOT stamp. A missed stamp is recoverable
                # with the backfill; a wrong one silently deletes revenue.
                _net_refunded = False
                print(f"⚠️ [mcp webhook] could not net-check refund for {_cust} "
                      f"({str(_ne)[:80]}) — NOT stamping")

            if _cust and _refunded_cents > 0 and _net_refunded:
                with _pool.connection() as _c:
                    with _c.cursor() as _cur:
                        # ★★MATCH ON AMOUNT, NOT JUST CUSTOMER. A customer can
                        # hold BOTH a refunded and a kept purchase — e.g.
                        # bryanseefeld95@gmail.com has a $49 developer row that
                        # was refunded and a $99 founding row that was NOT.
                        # Stamping every unstamped row for the customer would
                        # mark live revenue as refunded and UNDERSTATE MRR — the
                        # mirror image of the bug this is fixing.
                        # Stamp exactly ONE row: newest unstamped row whose
                        # booked mrr_cents equals the refunded amount.
                        # ★If nothing matches, stamp NOTHING and log loudly. A
                        # missed stamp is recoverable via
                        # backfill_conversion_refunds.py; a wrong stamp silently
                        # deletes real revenue from every report.
                        _cur.execute(
                            """UPDATE mcp_conversions
                                  SET refunded_at = NOW(),
                                      refunded_cents = %s
                                WHERE id = (
                                    SELECT id FROM mcp_conversions
                                     WHERE stripe_customer_id = %s
                                       AND refunded_at IS NULL
                                       AND COALESCE(mrr_cents, 0) = %s
                                     ORDER BY created_at DESC
                                     LIMIT 1)""",
                            (_refunded_cents, _cust, _refunded_cents))
                        _n = _cur.rowcount or 0
                if _n:
                    print(f"↩️ [mcp webhook] refund stamped: customer={_cust} "
                          f"amount={_refunded_cents/100:.2f}")
                else:
                    print(f"⚠️ [mcp webhook] refund for customer={_cust} "
                          f"amount={_refunded_cents/100:.2f} matched NO unstamped "
                          f"conversion row — NOT guessing. Reconcile with "
                          f"backfill_conversion_refunds.py")
            else:
                print(f"↩️ [mcp webhook] refund event with no customer/amount — "
                      f"nothing to stamp")
        except Exception as _re:
            # Never 500 a Stripe webhook: Stripe retries, and a retry storm on a
            # reporting-only stamp is worse than a missed stamp.
            print(f"⚠️ [mcp webhook] refund stamp failed: {str(_re)[:160]}")
        return jsonify({"received": True, "refund_handled": True}), 200

    if event_type == "checkout.session.completed":
        sess = event["data"]["object"] if isinstance(event, dict) else event.data.object
        sess = dict(sess) if not isinstance(sess, dict) else sess
        ref_session = sess.get("client_reference_id")
        cust_id     = sess.get("customer")
        cust_email  = ((sess.get("customer_details") or {}).get("email")
                       or sess.get("customer_email") or "").lower() or None
        # r89c (2026-06-14): WEB-source attribution. Pricing/SEO pages set
        # client_reference_id = ref_<source>__tool_<tool>__ts_<ts> (e.g.
        # ref_pricing-page__tool_none__ts_1781500890) — a DIFFERENT vocabulary from
        # an agent MCP session_id (a bare UUID). Parse which surface/tool drove the
        # sale; agent-session refs simply don't match this pattern (web_src=None).
        import re as _re_ref
        _wm = _re_ref.match(r'^ref_(.+?)__tool_(.+?)(?:__ts_\d+)?$', str(ref_session or ""))
        web_src  = _wm.group(1) if _wm else None
        web_tool = _wm.group(2) if _wm else None
        # per-surface-attr (2026-06-20): newer per-surface scheme
        # web__<surface>__<slug> (e.g. web__market__northern-virginia,
        # web__facility__a1b2c3d4, web__dcpi__ashburn, web__pricing__none) emitted
        # by routes/_attribution_ref.py so the operator can SEE which public page
        # drove the sale → web_source=<surface>, web_tool=<slug>. Parsed ONLY when
        # the legacy ref_ shape above didn't already match, and guarded inside
        # parse_web_ref to never swallow a DCM- pair-code / tu- top-up / ref_ ref
        # (those keep their existing handlers untouched).
        if not web_src:
            try:
                from routes._attribution_ref import parse_web_ref as _parse_web_ref
                _ws, _wt = _parse_web_ref(ref_session)
                if _ws:
                    web_src, web_tool = _ws, _wt
            except Exception:
                pass
        # sess-attr fix (2026-07-13, #1577 write-side): the agent paywall link
        # (routes/stripe_direct_upgrade.py) encodes the REAL Mcp-Session-Id as the
        # ':sess=<sid>' TAIL of a 'mcp:tool=…:ref=…:sess=<sid>' client_reference_id —
        # NOT as the whole cref. Matching the whole cref against
        # mcp_upgrade_signals.session_id (a bare UUID) never hit, so
        # attribution_signal_id was NULL on 100% of conversions. Resolve the sid
        # ONCE here; session_id_from_cref also returns a bare-UUID cref as-is
        # (server.mjs Fix E path, unchanged) and None for web/organic refs.
        try:
            from routes.conversion_attribution import session_id_from_cref as _sid_from_cref
            attr_sid = _sid_from_cref(ref_session)
        except Exception:
            attr_sid = None
        attribution, linked = {}, None
        try:
            from mcp_signal_canonical import mark_signals_converted
            attribution = mark_signals_converted(
                email=cust_email, stripe_customer_id=cust_id, session_id=attr_sid)
        except Exception as _e:
            attribution = {"error": str(_e)[:120]}
        try:
            if attr_sid:
                with _pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("""SELECT id FROM mcp_upgrade_signals
                                    WHERE session_id = %s ORDER BY created_at DESC LIMIT 1""",
                                (attr_sid,))
                    r = cur.fetchone()
                    if r:
                        cur.execute("""UPDATE mcp_conversions
                                          SET attribution_signal_id = %s,
                                              source = 'mcp_session_attributed'
                                        WHERE attribution_signal_id IS NULL
                                          AND (stripe_customer_id = %s
                                               OR LOWER(user_email) = COALESCE(%s, ''))""",
                                    (r[0], cust_id, cust_email))
                        linked = {"signal_id": r[0], "rows": cur.rowcount}
                        conn.commit()
        except Exception as _e:
            linked = {"error": str(_e)[:120]}
        # r89b (2026-06-14): mode=payment (one-time, e.g. Pro Annual $1,188) fires
        # checkout.session.completed but NEVER customer.subscription.created, so the
        # subscription-only INSERT (below, ~line 2128) never runs → these buyers were
        # invisible in mcp_conversions. (The main /webhook's phase17 INSERT targets a
        # non-existent schema and silently rolls back, so nothing covered them.)
        # Insert here, idempotently, WITH session attribution. mode=subscription is
        # intentionally SKIPPED — subscription.created owns that row (no double-count).
        onetime = None
        try:
            if (sess.get("mode") == "payment"
                    and (sess.get("payment_status") == "paid"
                         or sess.get("status") == "complete")
                    and not cust_email):
                # mcp_conversions.user_email is NOT NULL; a paid checkout with no
                # resolvable email is an anomaly (Stripe collects it on these links).
                # Record the skip rather than error the handler.
                onetime = {"skipped": "no_email"}
            elif (sess.get("mode") == "payment"
                    and (sess.get("payment_status") == "paid"
                         or sess.get("status") == "complete")):
                amount = int(sess.get("amount_total") or 0)
                # Our only payment-mode product is the annual prepay; normalize the
                # prepaid year to a monthly MRR so dashboards aren't inflated 12x. A
                # smaller one-time (no recurring meaning) contributes 0 MRR.
                if amount >= 100000:
                    plan_to, mrr = "pro_annual", round(amount / 12)
                else:
                    plan_to, mrr = "one_time", 0
                with _pool.connection() as conn, conn.cursor() as cur:
                    attr_id = None
                    if attr_sid:  # sess-attr fix: extracted sid, not the whole cref
                        cur.execute("""SELECT id FROM mcp_upgrade_signals
                                        WHERE session_id = %s ORDER BY created_at DESC LIMIT 1""",
                                    (attr_sid,))
                        _rr = cur.fetchone()
                        attr_id = _rr[0] if _rr else None
                    # source priority: agent signal > web ref > organic.
                    if attr_id:
                        _src = "mcp_session_attributed"
                    elif web_src:
                        _src = "web:" + web_src
                    else:
                        _src = "organic_no_mcp_touch"
                    # Idempotency key: the checkout session id (cs_…). Payment-mode
                    # rows have no subscription id, and stripe_subscription_id is the
                    # only UNIQUE column, so we store cs_ there to dedupe Stripe's
                    # webhook re-delivery. No reader filters on this column's shape.
                    cur.execute("""INSERT INTO mcp_conversions
                                     (user_email, caller_id, stripe_customer_id, stripe_subscription_id,
                                      plan_to, mrr_cents, source, attribution_signal_id,
                                      web_source, web_tool)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                   ON CONFLICT (stripe_subscription_id) DO NOTHING
                                   RETURNING id""",
                                (cust_email, (cust_email or '').strip().lower() or None,
                                 cust_id, sess.get("id"), plan_to, mrr,
                                 _src, attr_id, web_src, web_tool))
                    _row = cur.fetchone()
                    conn.commit()
                    onetime = {"conversion_id": (_row[0] if _row else None),
                               "plan_to": plan_to, "mrr_cents": mrr,
                               "attribution_signal_id": attr_id,
                               "web_source": web_src, "web_tool": web_tool,
                               "idempotent_skip": _row is None}
        except Exception as _e:
            onetime = {"error": str(_e)[:120]}
        # r89c: WEB-source attribution for mode=subscription (e.g. the metered link
        # bought from the pricing page). The canonical row is owned by
        # subscription.created (keyed on the subscription id); UPSERT on that SAME id
        # so both event orderings converge on ONE row. subscription.created's
        # ON CONFLICT touches only plan_to/mrr_cents, so these web fields survive; and
        # we never relabel a row already agent-attributed (attribution_signal_id set).
        webattr = None
        try:
            _sub = sess.get("subscription")
            if web_src and sess.get("mode") == "subscription" and _sub and cust_email:
                with _pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("""INSERT INTO mcp_conversions
                                     (user_email, caller_id, stripe_customer_id, stripe_subscription_id,
                                      plan_to, source, web_source, web_tool)
                                   VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)
                                   ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                                     caller_id  = COALESCE(mcp_conversions.caller_id, EXCLUDED.caller_id),
                                     web_source = EXCLUDED.web_source,
                                     web_tool   = EXCLUDED.web_tool,
                                     source = CASE WHEN mcp_conversions.attribution_signal_id IS NULL
                                                   THEN EXCLUDED.source ELSE mcp_conversions.source END
                                   RETURNING id""",
                                (cust_email, (cust_email or '').strip().lower() or None,
                                 cust_id, _sub, "web:" + web_src, web_src, web_tool))
                    _wr = cur.fetchone()
                    conn.commit()
                    webattr = {"web_source": web_src, "web_tool": web_tool,
                               "conversion_id": (_wr[0] if _wr else None)}
        except Exception as _e:
            webattr = {"error": str(_e)[:120]}
        return jsonify({"ok": True, "event": event_type,
                        "client_reference_id": ref_session,
                        "signal_attribution": attribution,
                        "conversion_linked": linked,
                        "onetime_conversion": onetime,
                        "web_attribution": webattr}), 200

    if event_type not in ("customer.subscription.created", "customer.subscription.updated"):
        return jsonify({"ok": True, "ignored": event_type}), 200

    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    obj = dict(obj) if not isinstance(obj, dict) else obj
    customer_id = obj.get("customer")
    sub_id      = obj.get("id")

    # Resolve customer email
    email = None
    try:
        api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if api_key and customer_id:
            stripe.api_key = api_key
            cust = stripe.Customer.retrieve(customer_id)
            email = (getattr(cust, "email", "") or "").lower() or None
    except Exception:
        pass
    if not email:
        return jsonify({"ok": False, "error": "couldnt resolve customer email"}), 200

    # Determine plan + MRR — metered-aware (r-mrr 2026-07-01). The old code
    # defaulted EVERY unlabeled subscription to plan_to='pro' / mrr_cents=4900,
    # so the $10 metered pack and annual prepays inflated recurring MRR with
    # fabricated fixed amounts. Rules now:
    #   • usage_type=metered → 'metered_usage' / 0 MRR (revenue is usage-billed)
    #   • yearly interval → normalize prepaid year to monthly (mirror the
    #     pro_annual amount/12 normalization in checkout.session.completed above)
    #   • label from lookup_key/nickname, else the price-id map, else 'unknown'
    #     — NEVER default to 'pro'.
    # Known price ids documented in routes/_stripe_links.py + routes/stripe_metered.py.
    _PRICE_ID_PLAN = {
        "price_1Tml5WJ9ey2ATcQlhqdF82z1": "pro_annual",     # $1,794/yr promo (_stripe_links.py)
        "price_1Tml5XJ9ey2ATcQl0pbU4htM": "founding",       # $99/mo founding (_stripe_links.py)
        "price_1TecqhJ9ey2ATcQl4Hmp99OU": "pro_annual",     # $1,188/yr campaign (campaign_halfprice_annual.py)
        "price_1TdNixJ9ey2ATcQldRAdlc7z": "metered_usage",  # metered price (stripe_metered.py)
        # 2026-07-10 (#1551): the $49/mo Developer price behind the paywall's
        # direct buy.stripe.com link has no lookup_key/nickname, so it recorded
        # as plan 'unknown' (mcp_conversions row 62). STRIPE_PRICE_DEV_MONTHLY.
        "price_1TB2WrJ9ey2ATcQlth13YBUT": "developer",      # $49/mo Developer (env STRIPE_PRICE_DEV_MONTHLY)
    }
    items = obj.get("items", {}).get("data", []) if obj.get("items") else []
    plan_to   = "unknown"
    mrr_cents = 0
    _label_defaulted = True
    _item0 = items[0] if (items and isinstance(items[0], dict)) else {}
    price  = _item0.get("price") or {}
    if price:
        _rec = price.get("recurring") or {}
        if _rec.get("usage_type") == "metered":
            plan_to, mrr_cents = "metered_usage", 0
            _label_defaulted = False
        else:
            mrr_cents = (price.get("unit_amount") or 0) * (_item0.get("quantity") or 1)
            if _rec.get("interval") == "year":
                mrr_cents = round(mrr_cents / 12)
            _label = (price.get("lookup_key") or price.get("nickname")
                      or _PRICE_ID_PLAN.get(price.get("id") or ""))
            if _label:
                plan_to, _label_defaulted = _label, False
    # 2026-07-10 (#1551): warn on EVERY unlabeled plan, not just mrr<$1 — the
    # old gate meant a real $49 sub recorded as 'unknown' silently (row 62).
    if _label_defaulted:
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "stripe subscription webhook: unlabeled plan (sub=%s price=%s) "
            "recorded as plan_to='unknown' mrr_cents=%s — audit the price in Stripe",
            sub_id, price.get("id"), mrr_cents)

    # Find most recent signal for this email (attribution)
    attribution_id = None
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT s.id FROM mcp_upgrade_signals s
                   LEFT JOIN mcp_dev_keys k ON k.api_key = s.session_id
                   WHERE COALESCE(s.user_email, k.email) = %s
                   ORDER BY s.created_at DESC LIMIT 1""",
                (email,),
            )
            row = cur.fetchone()
            attribution_id = row[0] if row else None
    except Exception:
        pass

    # r-attr 2026-06-12: honest channel label instead of the constant
    # "stripe_webhook_mcp". attribution_id is set only when a prior signal
    # matched this buyer's email — an MCP/agent-funnel touch. No match = the
    # buyer arrived organically (site/pricing) with no prior agent signal. The
    # email join is lossy (~99% of signals have NULL email), so 'organic' means
    # "no MCP touch found", not a hard claim. Proper pair-code attribution via
    # checkout.session.completed.client_reference_id is the deliberate next step.
    _source = "mcp_signal_attributed" if attribution_id else "organic_no_mcp_touch"
    # Insert (idempotent on stripe_subscription_id thanks to the UNIQUE constraint)
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_conversions
                     (user_email, caller_id, stripe_customer_id, stripe_subscription_id,
                      plan_to, mrr_cents, source, attribution_signal_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (stripe_subscription_id) DO UPDATE
                     SET plan_to   = EXCLUDED.plan_to,
                         mrr_cents = EXCLUDED.mrr_cents,
                         caller_id = COALESCE(mcp_conversions.caller_id, EXCLUDED.caller_id)
                   RETURNING id""",
                (email, (email or '').strip().lower() or None,
                 customer_id, sub_id, plan_to, mrr_cents,
                 _source, attribution_id),
            )
            conv_id = cur.fetchone()[0]
    except Exception as e:
        return jsonify({"error": "db insert failed", "detail": str(e)}), 500

    # r-provision (2026-06-16): make the paywall's "we email your API key right
    # after checkout" TRUE. The webhook recorded the conversion + bound the MCP
    # session, but never provisioned a DURABLE key — so subscription buyers (incl
    # the $1/100 metered plan, which IS a subscription) got only an ephemeral
    # in-session unlock and nothing in their inbox, and were locked out again next
    # session. Idempotently ensure a paid key for this email + email it on a fresh
    # mint. Best-effort: a provisioning failure must NEVER fail the webhook (the
    # conversion is already recorded above). Tier comes from Stripe's price
    # lookup_key (plan_to); metered/unknown keys default to the generous 'developer'
    # grant (500 calls/day) so the metered "no per-seat ceiling" pitch holds for
    # essentially every agent (a >500/day caller can be bumped).
    provisioned_key = None
    try:
        import secrets as _sec
        # mcp_dev_keys.tier has a CHECK constraint allowing ONLY free/paid/enterprise
        # (the gate maps the richer plan names — starter/developer/pro — onto 'paid').
        # Any paid subscription (incl the $1/100 metered plan) → 'paid'.
        _ptier = "enterprise" if (plan_to or "").lower() == "enterprise" else "paid"
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT api_key, tier FROM mcp_dev_keys "
                        "WHERE LOWER(email)=%s AND status='active' "
                        "ORDER BY created_at DESC LIMIT 1", (email,))
            _ex = cur.fetchone()
            _newmint = False
            _upgraded = False
            if _ex:
                provisioned_key = _ex[0]
                if (_ex[1] or "free").lower() in ("free", "trial", "anon", ""):
                    cur.execute("UPDATE mcp_dev_keys SET tier=%s WHERE api_key=%s",
                                (_ptier, provisioned_key))
                    conn.commit()
                    _upgraded = True  # free/trial key just became paid → deliver it
            else:
                provisioned_key = "dch_live_" + _sec.token_hex(16)
                _newmint = True
                cur.execute("""INSERT INTO mcp_dev_keys
                                 (api_key, developer_id, email, tier, status, metadata)
                               VALUES (%s, %s, %s, %s, 'active', %s::jsonb)""",
                            (provisioned_key, "dev_" + _sec.token_hex(8), email, _ptier,
                             json.dumps({"source": "stripe_subscription",
                                         "stripe_customer_id": customer_id,
                                         "stripe_subscription_id": sub_id})))
                conn.commit()
        # Email the key on a fresh mint OR when a buyer's pre-existing free/trial key
        # was just upgraded to paid. The upgrade case is the white-glove gap that bit
        # our first Pro customer (eren@globeholder.ai, 2026-07-10): she claimed a free
        # key BEFORE paying, so the sub webhook reused it and — under the old
        # `_newmint`-only guard — sent NO welcome, leaving a paying customer who never
        # received her key. mcp_dev_keys stores api_key in plaintext, so we can deliver
        # the actual key even on reuse. Both branches are one-time state transitions
        # (mint / free→paid), so Stripe webhook re-delivery is a no-op email-wise: the
        # 2nd delivery finds tier already 'paid' → no upgrade → no email. Lazy import of
        # main (main imports us, so a top-level import would be circular).
        if (_newmint or _upgraded) and provisioned_key:
            try:
                import main as _main_mod
                # r-claim (2026-08-17): provenance rides plan_name and the
                # sender's own atomic claim writes THE one log row. The
                # separate audit row this block used to add double-counted
                # every send (the "second log row" the onboarding shell
                # flagged) and — worse — logged 'sent' for a fire-and-forget
                # thread that had not sent anything yet.
                # r-entry-path (2026-08-19): mint the 72h set-password link.
                # THIS is the path a buyer who had a free account first takes,
                # and it used to omit reset_url entirely — so those customers
                # received a key and no way into the dashboard, while a COLD
                # buyer (main.py's new-user branch) got the button. That
                # asymmetry is what made rob@hedmarkholdings.com email support
                # asking how to reset his password 51 minutes after paying.
                # mint_reset_url returns None if the token did not persist, and
                # send_welcome_email_sendgrid already renders the no-link
                # variant on None — so a DB blip degrades to today's behaviour
                # instead of shipping a dead link.
                from routes._password_reset_link import mint_reset_url
                _main_mod.send_welcome_email_sendgrid(
                    email, provisioned_key,
                    plan_name=f"{_ptier}:{'mint' if _newmint else 'upgrade'}",
                    reset_url=mint_reset_url(email))
            except Exception as _ee:
                # send_welcome_email_sendgrid already admin-alerts on SendGrid
                # failure; swallow here so provisioning never breaks the webhook.
                pass
    except Exception:
        note_swallowed_write("mcp_dev_keys", where="flask_mcp_endpoints.stripe_webhook_mcp")
        pass

    # r68-canonical (2026-06-02): write-back attribution on signals.converted.
    # Previously this webhook recorded mcp_conversions but never flipped
    # signals.converted, so /api/v1/mcp/funnel kept reporting 0% on `mcp`
    # platform forever. Now matches by email OR session OR caller_id OR
    # stripe_customer_id so the ~99% of `mcp` signals with NULL email also get flipped.
    try:
        from mcp_signal_canonical import mark_signals_converted
        signal_attribution = mark_signals_converted(
            email=email,
            stripe_customer_id=customer_id,
            session_id=(obj.get('metadata') or {}).get('mcp_session_id'),
        )
    except Exception as _e:
        signal_attribution = {'error': str(_e)[:120]}

    # r-keybound-platform (2026-07-18, #1660 residual): stamp the buyer's
    # dominant agent PLATFORM (per-key call history: email → their key →
    # mcp_call_log platform tags) onto the conversion row when it carries no
    # already-set platform. Additive, POST-credit (the conversion, key
    # provisioning, and signal flips above are already committed) and
    # COALESCE-only — and fully fail-soft: attribution can NEVER fail the
    # payment webhook.
    _kp_platform = None
    try:
        from mcp_signal_canonical import (resolve_key_platform as _rkp,
                                          stamp_conversion_platform as _scp)
        _kp = _rkp(email=email)
        _kp_platform = _kp.get('platform')
        if _kp_platform:
            _scp(stripe_ref=sub_id, platform=_kp_platform)
    except Exception as _kpe:
        print(f"⚠️ keybound platform stamp failed (non-fatal): {_kpe}")

    return jsonify({
        "ok":                    True,
        "conversion_id":         conv_id,
        "email":                 email,
        "attribution_signal_id": attribution_id,
        "plan_to":               plan_to,
        "mrr_cents":             mrr_cents,
        "signal_attribution":    signal_attribution,
    }), 200



# ── GET /api/v1/dev-signup-form — Serve the widget HTML through Flask ─────

@mcp_bp.get("/api/v1/dev-signup-form")
def dev_signup_form():
    """Serve the standalone signup widget. Embed via iframe or link from /ai."""
    try:
        with open("static/signup-widget.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("<h1>Signup widget not deployed</h1>", status=404, mimetype="text/html")



# ── GET /api/v1/_env_stripe_check — verify Stripe env vars are loaded ──────

@mcp_bp.get("/api/v1/_env_stripe_check")
@_require_internal
def env_stripe_check():
    """Diagnostic: is STRIPE_WEBHOOK_SECRET_MCP loaded? (no secret values exposed)"""
    sec = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP", "")
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    return jsonify({
        "STRIPE_WEBHOOK_SECRET_MCP_set":         bool(sec),
        "STRIPE_WEBHOOK_SECRET_MCP_length":      len(sec) if sec else 0,
        "STRIPE_WEBHOOK_SECRET_MCP_prefix":      (sec[:6] + "…") if sec else None,
        "STRIPE_SECRET_KEY_set":                 bool(key),
        "STRIPE_SECRET_KEY_prefix":              (key[:7] + "…") if key else None,
        "all_env_vars_starting_with_STRIPE":     sorted([
            k for k in os.environ.keys() if k.upper().startswith("STRIPE")
        ]),
    }), 200



# ── POST /api/v1/mcp/trial-check — has this session used its trial? ────────

@mcp_bp.post("/api/v1/mcp/trial-check")
@_require_internal
def trial_check():
    """server.mjs calls this to ask: has session_id already consumed its
    free preview for this tool? Returns {trial_used, prior_calls,
    tier_upgrade}.

    A trial is "used" if mcp_call_log has any prior status='ok' OR
    status='trial_used' row for the same (session_id, tool) combo.

    r41-session-upgrade (2026-05-25): also returns `tier_upgrade` when
    the session has a redeemed dev key. When a Claude.ai web user hits
    a paywall and follows the redeem URL, the redeem handler at
    routes/redeem_routes.py creates a dev key with
    metadata.session_id = <this session_id>. Subsequent paid-tool
    attempts in the SAME chat session can then be upgraded in-place,
    closing the Claude.ai gap (their custom-connector UI can't attach
    an X-API-Key header, so without this their session is stuck at
    free tier forever).

    server.mjs treats tier_upgrade as a directive to update
    sessionMeta.tier — see r41-session-upgrade in server.mjs.
    """
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    tool = body.get("tool")
    if not session_id or not tool:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "reason": "missing_session_or_tool"}), 200

    out = {"trial_used": True, "prior_calls": 0}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Trial-eligibility check (existing behavior)
            cur.execute(
                """SELECT COUNT(*) FROM mcp_call_log
                   WHERE session_id = %s
                     AND tool = %s
                     AND status IN ('ok', 'trial_used')""",
                (session_id, tool),
            )
            prior = cur.fetchone()[0]
            out["trial_used"]  = prior > 0
            out["prior_calls"] = prior

            # r41-session-upgrade: was this session redeemed? Look for a
            # dev key whose JSON metadata records this session_id. The
            # metadata column is JSONB so the ->> operator gives O(log n)
            # lookup with a GIN index, or O(n) sequential scan. With ~13
            # paid keys total, even a sequential scan is sub-millisecond.
            try:
                cur.execute(
                    """SELECT plan
                       FROM api_keys
                       WHERE metadata::jsonb ->> 'session_id' = %s
                         AND is_active = 1
                       ORDER BY id DESC
                       LIMIT 1""",
                    (session_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    plan = str(row[0]).lower()
                    # Only suggest an upgrade for plans that actually
                    # unlock paid tools — never accidentally "upgrade"
                    # someone to a lower tier than they already have.
                    if plan in ('developer', 'pro', 'enterprise', 'founding'):
                        out["tier_upgrade"] = plan
            except Exception:
                # Schema variants in the wild (metadata stored as TEXT
                # in some envs vs JSONB in others). Don't fail the whole
                # trial-check on a session-upgrade lookup error.
                pass

            # Fix E (2026-06-06): mcp_session_upgrades closure ──────────
            # The Stripe checkout.session.completed webhook
            # (handle_checkout_completed in main.py) writes a row keyed by
            # mcp_session_id whenever the human pays via a paywall link
            # whose client_reference_id matched this MCP session. Look
            # for a row touched in the last 1h that hasn't been "consumed"
            # by an earlier trial-check call yet. When found, return
            # tier_upgrade=<plan> so server.mjs flips this session's tier
            # in-place on the very next paid-tool call — no key swap, no
            # reconnect. Only overrides the api_keys lookup above when it
            # found nothing, so existing redeem-key flows are preserved.
            try:
                cur.execute(
                    """SELECT plan FROM mcp_session_upgrades
                       WHERE mcp_session_id = %s
                         AND upgraded_at > NOW() - INTERVAL '1 hour'
                       LIMIT 1""",
                    (session_id,),
                )
                _u_row = cur.fetchone()
                if _u_row and _u_row[0]:
                    _u_plan = str(_u_row[0]).lower()
                    # Map plan_name → tier (server.mjs accepts developer/pro/enterprise/founding)
                    _plan_to_tier = {
                        'starter':    'developer',  # $9 Starter unlocks paid tools = developer-equivalent
                        'developer':  'developer',
                        'pro':        'pro',
                        'pro_monthly':  'pro',
                        'pro_annual':   'pro',
                        'enterprise': 'enterprise',
                        'enterprise_monthly': 'enterprise',
                        'enterprise_annual':  'enterprise',
                        'founding':   'pro',
                    }
                    _u_tier = _plan_to_tier.get(_u_plan, _u_plan)
                    if _u_tier in ('developer', 'pro', 'enterprise', 'founding'):
                        # Don't downgrade if api_keys lookup already
                        # produced a stronger tier (rare race, but possible).
                        _existing = out.get('tier_upgrade', '')
                        _rank = {'developer': 1, 'founding': 2, 'pro': 2, 'enterprise': 3}
                        if _rank.get(_u_tier, 0) >= _rank.get(_existing, 0):
                            out["tier_upgrade"] = _u_tier
                            out["fix_e_session_bound"] = True
                            # mark consumed so we don't re-flip on every call
                            try:
                                cur.execute(
                                    """UPDATE mcp_session_upgrades
                                       SET consumed_at = COALESCE(consumed_at, NOW())
                                       WHERE mcp_session_id = %s""",
                                    (session_id,),
                                )
                                conn.commit()
                            except Exception:
                                try: conn.rollback()
                                except Exception: pass
            except Exception:
                # Table may not exist yet (pre-schema-repair) — soft-fail.
                try: conn.rollback()
                except Exception: pass

            # keystone (audit item 1, 2026-06-30): FREE-IDENTIFIED session bind.
            # claim_key stamps metadata.session_id onto the key it mints. If THIS
            # session has such a key, hand server.mjs the api_key + its tier so it
            # binds the session durably (cross-replica) on the next call — the fix
            # for claim_free_key returning auto_applied_to_session:false / next call
            # _bind-only. Recency-bounded (24h) so the scan stays cheap; only fills
            # in when a stronger PAID upgrade wasn't already resolved above.
            try:
                if not out.get("tier_upgrade") or str(out.get("tier_upgrade")).lower() == "identified":
                    cur.execute(
                        """SELECT api_key, tier FROM mcp_dev_keys
                           WHERE metadata->>'session_id' = %s
                             AND status = 'active'
                             AND created_at > NOW() - INTERVAL '24 hours'
                           ORDER BY created_at DESC
                           LIMIT 1""",
                        (session_id,),
                    )
                    _sk = cur.fetchone()
                    if _sk and _sk[0]:
                        out["session_api_key"] = _sk[0]
                        out["tier_upgrade"] = (_sk[1] or "identified")
                        out["session_bound_free"] = True
            except Exception:
                try: conn.rollback()
                except Exception: pass

        return jsonify(out), 200
    except Exception as e:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "error": str(e)}), 200


# ── GET /api/v1/stats/live-proof — HONEST platform-usage proof ──────────────
# Master-shell 2 (2026-06-02): the testimonials / social-proof wall must be
# REAL platform usage, never invented. This endpoint returns counts that ALL
# trace to a live DB read; if a value is 0 or its source table/column is
# unavailable, it returns 0 / null with an explicit flag instead of a
# fabricated number.
#
# LIVE-SCHEMA TRUTH (verified via information_schema 2026-06-02 against the
# Railway origin — /api/v1/admin/schema?table=mcp_tool_calls):
#   mcp_tool_calls(id, tool_name, platform, client_name, params, success,
#                  response_time_ms, ip_address, user_agent, created_at,
#                  session_id)            -- created_at = timestamp col
#   ai_testimonials(... approved, source, created_at ...)
#
# Columns this endpoint reads (cited back in the response as source_columns):
#   tool_calls_7d / 30d  : COUNT(*)               WHERE created_at >= NOW()-N
#   distinct_callers_7d  : COUNT(DISTINCT agent_id) mcp_calls_identity, real
#                          external only -- NEVER session_id (rotates per conn)
#   distinct_ips_7d      : COUNT(DISTINCT client_ip), public only -- secondary
#   distinct_platforms   : COUNT(DISTINCT platform) minus internal/probe/
#                          generic buckets (honest external-vendor count)
#   approved_testimonials_count : COUNT(*) FROM ai_testimonials
#                                 WHERE approved = TRUE
#
# This is the source of truth for the frontend "N agents use DC Hub" headline
# and for /api/agents/registry's de-hardcoded counts.

# Buckets that are NOT external AI platforms — our own infra, probes,
# health-checkers, scrapers, and the generic 'mcp'/'unknown' transport
# placeholders. Mirrors the funnel's _signal_excl conventions so the
# platform count is an honest external-vendor number, not inflated by
# our own traffic. (Memory: dchub MCP signal-inflation — gate on real
# external callers, never raw COUNT(*).)
_LIVE_PROOF_NONPLATFORM = (
    "", "mcp", "mcp-worker", "mcp_generic", "mcp-generic", "unknown",
    "unknown-ua", "anonymous", "internal", "internal-dchub", "direct",
    "node-script", "python-script", "curl", "postman", "probe",
    "health", "scanner", "checker",
)

# Pattern guard for DC Hub's OWN internal traffic (self-heal/pipeline/loops/
# probes) so the "N platforms" headline reflects EXTERNAL adoption only, never
# self-inflated (the signal-inflation rule). Mirrors agent_network_effect.
# Substring markers of DC Hub self / test / probe traffic — matched ANYWHERE (the
# inflators carried the marker mid-string), so the "N platforms" headline counts
# only real EXTERNAL agents. Real AI platforms (claude/chatgpt/...) contain none.
_LP_INTERNAL_MARKERS = (
    "dchub", "selfheal", "self-heal", "self_heal", "pipeline", "probe",
    "scanner", "checker", "monitor", "health", "heartbeat", "loop",
    "local-agent", "localhost", "127.0.0.1", "smoke", "warmup", "sentinel",
    "remote0", "remote1", "step2", "step3", "_test", "-test", "test-", "test_",
    "verify",  # 'verify' probes (e.g. claude-code-verify-0701) were passing as real
)
def _lp_is_internal(p):
    if not p:
        return True
    p = p.lower()
    if p in _LIVE_PROOF_NONPLATFORM or len(p) < 3:
        return True
    return any(m in p for m in _LP_INTERNAL_MARKERS)


# Allowlist of recognized external AI platforms (substring match). distinct_platforms
# counts ONLY these — a denylist can't keep up with the audit/test long tail
# (Scraper-Block-Verify, Leakaudit4, single-char noise, ...). Conservative: a new
# platform not yet listed is undercounted, never noise-inflated.
#
# ★2026-09-05 — NOW IMPORTED, NOT COPIED. ai_platform_canon's docstring has said
# "those now import from here" since 2026-07-27; it was not true. This module and
# agent_network_effect each kept a byte-identical literal tuple, so the canon was
# the single source of truth for the COUNT (`count_platforms`, used by
# distinct_platforms below) and not for the LIST (`platforms_30d`, filtered by
# _lp_is_recognized right here). Adding 'codex' to the canon therefore made one
# payload contradict itself: the platform was counted in distinct_platforms and
# absent from platforms_30d in the same response. Re-sourced so the three can
# never diverge again; tests/test_platform_canon_single_source.py fails if a
# fourth copy appears.
from ai_platform_canon import KNOWN_AI_TOKENS as _LP_KNOWN_AI_TOKENS
def _lp_is_recognized(p):
    if not p:
        return False
    p = p.lower()
    return any(tok in p for tok in _LP_KNOWN_AI_TOKENS)


@mcp_bp.get("/api/v1/stats/live-proof")
def stats_live_proof():
    """PUBLIC. Real platform-usage proof — every number traces to a DB read.

    Returns 0 / null + a `data_available: false` flag rather than ever
    inventing a count. No PII (no emails, no IPs returned — only an
    aggregate distinct-IP COUNT). Safe to render publicly.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    out = {
        "ok": True,
        "as_of": now_iso,
        "data_available": False,          # flips true if ANY real read works
        # Honest defaults — these are NOT displayed as real unless the
        # matching *_available flag is true.
        "tool_calls_7d": 0,
        "tool_calls_30d": 0,
        "distinct_callers_7d": 0,
        "distinct_ips_7d": 0,
        "distinct_platforms": 0,
        "approved_testimonials_count": 0,
        "platforms_30d": [],              # [{platform, calls, calls_including_self_traffic,
                                          #   active_days, last_call, recurring}]
        "platforms_30d_excluded": {},     # what the self-traffic filter removed
        "flags": {
            "tool_calls_available": False,
            "callers_available": False,
            "platforms_available": False,
            "testimonials_available": False,
        },
        # Cite the exact live columns each number is derived from.
        "source_columns": {
            "tool_calls_7d":               "COUNT(*) mcp_tool_calls WHERE created_at >= NOW()-7d",
            "tool_calls_30d":              "COUNT(*) mcp_tool_calls WHERE created_at >= NOW()-30d",
            "distinct_callers_7d":         "COUNT(DISTINCT agent_id) mcp_calls_identity (7d) WHERE is_public_ip AND is_real_external — canonical identity view; NEVER session_id (it rotates per MCP connection)",
            "distinct_ips_7d":             "COUNT(DISTINCT client_ip) mcp_calls_identity (7d) WHERE is_public_ip — all traffic incl. probes, secondary signal",
            "distinct_platforms":          "COUNT(DISTINCT recognized vendor) mcp_calls_identity (30d) WHERE is_public_ip AND is_real_external, minus declared operator self-traffic — was raw mcp_tool_calls with neither filter until 2026-09-03",
            "approved_testimonials_count": "COUNT(*) ai_testimonials WHERE approved = TRUE",
        },
        "note": ("All counts are live reads from mcp_tool_calls, "
                 "mcp_calls_identity + ai_testimonials. A value of 0 with its "
                 "*_available flag false means no data / table unavailable — "
                 "never a placeholder. platforms_30d counts EXTERNAL calls "
                 "only; each row carries its unfiltered sibling and "
                 "platforms_30d_excluded says what was removed."),
    }

    # 1) Tool-call volume (7d / 30d) — created_at is the verified ts column.
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d"] = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["tool_calls_30d"] = int((cur.fetchone() or [0])[0] or 0)
        out["flags"]["tool_calls_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["tool_calls_error"] = str(e)[:120]

    # 2) Distinct callers — the CANONICAL identity view (agent_id = md5 of the
    #    first public XFF token, real-external only). NEVER session_id: the
    #    server mints a new session per MCP connection, so DISTINCT session_id
    #    tracks call volume, not callers (r-reach-canonical-views 2026-07-01;
    #    session-counting read 1,792 "callers" where the honest count was ~14).
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT agent_id) FILTER (WHERE is_real_external), "
                "       COUNT(DISTINCT client_ip) "
                "FROM mcp_calls_identity "
                "WHERE created_at >= NOW() - INTERVAL '7 days' AND is_public_ip"
            )
            r = cur.fetchone() or (0, 0)
            out["distinct_callers_7d"] = int(r[0] or 0)
            out["distinct_ips_7d"] = int(r[1] or 0)
        out["flags"]["callers_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["callers_error"] = str(e)[:120]

    # 3) Distinct EXTERNAL platforms (30d) — exclude our own infra / probes /
    #    generic transport buckets so the "N platforms" headline is honest.
    # ★ 2026-09-03: this counted RAW mcp_tool_calls — no is_real_external, no
    # self-traffic exclusion — while /api/ai/tracking's cards counted the same
    # 30 days through mcp_calls_identity with both filters applied. Same vendor
    # collapsing on both sides, so the gap was never a double-count: it was
    # OUR OWN traffic. Measured that day, Claude read 1,071 here against 492 on
    # its card, and the 579-call gap grew ~81 in eight hours.
    #
    # The operator's agent client writes mcp_client 'claude' / user_agent
    # 'node', byte-identical to a prospect's — which is exactly why the funnel
    # on the same page carries DEFINITION v4 and excludes it from "Human
    # acted". That lesson was never applied to the headline that names which
    # platforms integrate. It is applied here now, through the SAME declared
    # vocabulary rather than a second copy of it.
    try:
        try:
            from mcp_calls_deloop import (external_session_predicate,
                                          self_traffic_session_prefixes)
            _not_self = external_session_predicate("session_id")
            _prefixes = list(self_traffic_session_prefixes())
        except Exception as _pe:
            # Fail OPEN, never silently: nothing is removed and the payload
            # says so, so a reader can tell "no exclusion applied" from
            # "applied, found nothing". Same contract as ai_tracking's envelope.
            _not_self, _prefixes = "TRUE", []
            out["flags"]["platforms_self_traffic_filter"] = (
                "unavailable, nothing excluded: %s" % str(_pe)[:80])
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                # 30d window (was 7d): platforms = BREADTH of who integrates, so a
                # longer lookback is the honest, representative figure — 7d undersold
                # at 3 because Claude/Anthropic dominate recent traffic. (Callers/IPs
                # stay 7d: those measure recent activity, not breadth.)
                # r-burst-vs-adoption (2026-09-04): active_days + last_call
                # ride along so shape_platforms can tell a platform that
                # INTEGRATED from one that ran a single test. Both are
                # computed over the SAME filtered population as `n` — a
                # day counted here must be a day that survived the
                # self-traffic filter, or "10 active days" could be nine of
                # ours plus one real.
                "SELECT LOWER(COALESCE(platform, '')) AS p, "
                "       COUNT(*) FILTER (WHERE " + _not_self + ") AS n, "
                "       COUNT(*) AS n_gross, "
                "       COUNT(DISTINCT created_at::date) "
                "         FILTER (WHERE " + _not_self + ") AS active_days, "
                "       MAX(created_at) FILTER (WHERE " + _not_self + ")"
                "         ::date AS last_call "
                "FROM mcp_calls_identity "
                "WHERE is_public_ip AND is_real_external "
                "  AND created_at >= NOW() - INTERVAL '30 days' "
                "GROUP BY p ORDER BY n DESC"
            )
            rows = cur.fetchall() or []
        recognized = [
            (p, n, g, ad, lc)
            for (p, n, g, ad, lc) in rows
            # Allowlist: recognized AI platform AND not internal/probe traffic.
            if _lp_is_recognized(p) and not _lp_is_internal(p)
            # drop UUID-shaped session leakage that escaped normalization
            and not _UUID_RE_MOD.match(p)
        ]
        # ★ The rule that a platform whose every call was OURS is not a
        # platform lives in its own import-free module. A judgement inline in a
        # route is a judgement nobody can test: written here first, it survived
        # every source-text guard aimed at this endpoint — a mutation that put
        # those rows back into the headline passed all of them.
        from live_proof_platforms import shape_platforms
        externals, out["platforms_30d_excluded"] = shape_platforms(
            recognized, _prefixes)
        out["platforms_30d"] = externals
        # ★★2026-07-27: was len(externals) — the count of distinct raw platform
        # STRINGS that passed the allowlist. That still multiple-counts a single
        # vendor: `claude`, `claude-code`, `anthropic/claudeai` and
        # `anthropicapi` are all Anthropic, and each was its own "platform".
        # Count canonical VENDORS instead, through the same module
        # /api/v1/ai/reach uses, so the two endpoints cannot drift apart again
        # (they previously published 10 over 30 days against 15 over 7 — a
        # longer window reporting fewer platforms, which is impossible).
        # platforms_30d keeps the raw ids; only the headline count collapses.
        from ai_platform_canon import count_platforms
        out["distinct_platforms"] = count_platforms(e["platform"] for e in externals)
        out["flags"]["platforms_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["platforms_error"] = str(e)[:120]

    # 4) Approved public testimonials — the only ones safe to show.
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ai_testimonials WHERE approved = TRUE"
            )
            out["approved_testimonials_count"] = int((cur.fetchone() or [0])[0] or 0)
        out["flags"]["testimonials_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["testimonials_error"] = str(e)[:120]

    return jsonify(out), 200


# ── GET /api/v1/reach — HONEST agent-reach dashboard data ───────────────────
# The 2026-06-30 growth audit's #1 finding: DC Hub could not MEASURE its binding
# constraint (real AI agents reaching the MCP server) from inside — get_agent_registry
# returned a static roster and the public headline conflated real agents with probe
# noise. r-reach-identity (2026-07-01) fixed two further inflations, verified live:
#   (1) "agents" was COUNT(DISTINCT session_id), but session_id rotates per MCP
#       connection (real-external sessions average ~1.2 calls; 1 of 7,933 sessions
#       in 30d spanned more than one day), so agents ≈ calls (96 calls read as 67
#       "agents" from ~14 real IPs). Agent identity is now the first token of
#       ip_address (some rows hold raw X-Forwarded-For chains), private/CGNAT
#       excluded — the same identity as the mcp_calls_identity /
#       mcp_agent_retention_30d views.
#   (2) the real-vs-probe split was a local platform-column allowlist with no
#       user-agent guard, so curl/urllib probes self-labelled 'claude-desktop' /
#       'ChatGPT' / 'openai-eval' passed as real. The split now uses
#       mcp_calls_deloop.real_calls_predicate() — the same canonical filter as
#       /api/v1/mcp/funnel and routes/funnel_health.
# Every number is a live DB read; a missing table fails soft to 0 + a flag
# (never a placeholder). Public + no PII (IPs are aggregated, never returned).
_REACH_STMT_TIMEOUT_MS = 4000


def _reach_bounded(cur, sql, fetch="all"):
    """Run ONE aggregate inside its own explicit transaction with
    SET LOCAL statement_timeout — the only form that sticks on Neon's
    POOLED endpoint (pgbouncer transaction mode rejects startup options
    at connect, and a plain session SET lands on a different backend
    connection than the query; see routes/funnel_health._bounded,
    verified live 2026-07-01). Deploy/DB-contention windows pushed this
    endpoint to 43-55s (Railway HTTP logs 2026-07-02) — far past the
    edge worker's 5s/15s attempt budget, so reach.html rendered a fetch
    error. A stalled aggregate now fails at ~4s into the existing flags
    envelope instead. The connection is autocommit, so BEGIN/COMMIT are
    explicit; ROLLBACK on error so a timed-out query never poisons the
    next one."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _REACH_STMT_TIMEOUT_MS)
        cur.execute(sql)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _reach_build_data():
    # r-reach-canonical-views (2026-07-01): agent counts read the CANONICAL
    # identity views (mcp_calls_identity / mcp_agent_retention_30d) instead of
    # re-deriving identity + real-vs-probe inline. One definition of "real
    # external agent" lives in the DB view (agent_id = md5(first XFF token),
    # is_public_ip + is_real_external filters); every surface that reports
    # agents must read it — session_id is NEVER an agent (it rotates per MCP
    # connection). If the views are missing this endpoint fails soft to 0 +
    # calls_error, loudly, rather than silently re-inventing its own count.
    _real = "is_real_external"
    _pub = "is_public_ip"

    def _from(window_sql):
        return ("(SELECT client_name, user_agent, platform, tool_name, "
                "        agent_id, client_ip, is_public_ip, is_real_external "
                "   FROM mcp_calls_identity WHERE " + window_sql + ") t")

    _7d = "created_at >= NOW() - (7 * INTERVAL '1 day')"
    _prev7d = ("created_at >= NOW() - (14 * INTERVAL '1 day') "
               "AND created_at < NOW() - (7 * INTERVAL '1 day')")

    out = {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "data_available": False,
        "binding_constraint": "reach — real AI agents/week that call the MCP server",
        "real_agents_7d": 0, "real_calls_7d": 0,
        "probe_agents_7d": 0, "probe_calls_7d": 0,
        "real_share_pct": None,          # real_calls / (real+probe) — how much traffic is genuine
        "wow": {"real_agents_pct": None, "real_calls_pct": None},
        "platforms_7d": [],              # real (de-looped) traffic only
        "top_tools_7d": [],              # real traffic only
        # instrument-before-spend (2026-07-20): calls-per-agent DEPTH distribution
        # over the canonical identity view (7d, real+public agents). Answers "is the
        # growth lever depth-per-visit or 2nd-day return?" — sits next to
        # retention_30d.day2_return_rate_pct so the two levers are directly
        # comparable. Additive; never replaces an existing field.
        "depth_per_agent_7d": {"agents": 0, "median_calls": None, "p90_calls": None,
                               "mean_calls": None, "max_calls": 0},
        "retention_30d": {"agents": 0, "returned_2nd_day": 0, "day2_return_rate_pct": None},
        "citations_7d": 0, "citation_engines_7d": 0,
        "flags": {"calls_available": False, "retention_available": False,
                  "citations_available": False},
        "source_columns": {
            "real_agents_7d": "COUNT(DISTINCT agent_id) mcp_calls_identity (7d) WHERE is_public_ip AND is_real_external — the canonical identity view (agent_id = md5(first X-Forwarded-For token))",
            "real_calls_7d":  "COUNT(*) mcp_calls_identity (7d) WHERE is_real_external",
            "probe_calls_7d": "COUNT(*) mcp_calls_identity (7d) WHERE NOT is_real_external (internal/probe/self-heal/scripted-UA)",
            "depth_per_agent_7d": "PERCENTILE_CONT median + p90 of calls-per-agent over mcp_calls_identity (7d) WHERE is_real_external AND is_public_ip, grouped by agent_id — depth-per-visit lever vs the day2_return retention lever",
            "retention_30d":  "mcp_agent_retention_30d — per-agent active_days over 30d (canonical retention view)",
            "citations_7d":   "COUNT(*) ai_citations (7d) WHERE dchub_cited = true",
        },
        "note": ("Agent counts read the canonical DB views mcp_calls_identity / "
                 "mcp_agent_retention_30d (identity = md5 of the first X-Forwarded-For "
                 "token, private/CGNAT excluded, is_real_external filters probe/self/"
                 "scripted-UA traffic) — never session_id, which rotates per MCP "
                 "connection and tracks call volume, not agents. "
                 "0 with a false flag means no data, never a placeholder."),
    }
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # 7d totals — real vs probe; agents = distinct agent_id from the
            # canonical identity view (md5 of the first public XFF token).
            r = _reach_bounded(cur,
                "SELECT COUNT(*) FILTER (WHERE (" + _real + ")), "
                "       COUNT(DISTINCT agent_id) FILTER (WHERE (" + _real + ") AND " + _pub + "), "
                "       COUNT(*) FILTER (WHERE NOT (" + _real + ")), "
                "       COUNT(DISTINCT agent_id) FILTER (WHERE NOT (" + _real + ") AND " + _pub + ") "
                "FROM " + _from(_7d), fetch="one") or (0, 0, 0, 0)
            rc, rag, pc, pag = (int(v or 0) for v in r)
            out["real_calls_7d"], out["real_agents_7d"] = rc, rag
            out["probe_calls_7d"], out["probe_agents_7d"] = pc, pag
            tot = rc + pc
            out["real_share_pct"] = round(100.0 * rc / tot, 1) if tot else None
            # platform breakdown — real traffic only, canonical classifier
            rows = _reach_bounded(cur,
                "SELECT (" + _DELOOP_PLATFORM_CASE.strip() + ") AS p, COUNT(*) AS calls, "
                "       COUNT(DISTINCT agent_id) FILTER (WHERE " + _pub + ") AS agents "
                "FROM " + _from(_7d) + " WHERE (" + _real + ") "
                "GROUP BY 1 ORDER BY calls DESC LIMIT 20")
            out["platforms_7d"] = [
                {"platform": p, "calls": int(calls or 0), "agents": int(agents or 0)}
                for (p, calls, agents) in (rows or [])]
            # prior 7d (days 7-14) for week-over-week
            r = _reach_bounded(cur,
                "SELECT COUNT(*) FILTER (WHERE (" + _real + ")), "
                "       COUNT(DISTINCT agent_id) FILTER (WHERE (" + _real + ") AND " + _pub + ") "
                "FROM " + _from(_prev7d), fetch="one") or (0, 0)
            prc, prag = int(r[0] or 0), int(r[1] or 0)
            def _delta(cur_v, prev_v):
                return round(100.0 * (cur_v - prev_v) / prev_v, 1) if prev_v else None
            out["wow"]["real_agents_pct"] = _delta(rag, prag)
            out["wow"]["real_calls_pct"] = _delta(rc, prc)
            # ★ 2026-08-20 — THE THIRD SURFACE, FOUND BY NULLING THE OTHER TWO.
            # static/mcp-dashboard.html walks a FALLBACK CHAIN for its agents
            # WoW: complete-week key -> rolling key -> fetch /api/v1/reach and
            # read wow.real_agents_pct. #2978 withheld the first two across
            # #202 (2026-08-18 06:31Z) — which routed the dashboard straight to
            # this pair, unguarded, and it rendered -26.6% with no caveat. Same
            # rolling 7d-vs-prior-7d windows as the funnel pair, so the same
            # hazard: the current window CONTAINS the correction.
            # Guarding the DATA rather than the renderer is what makes the
            # fallback chain safe — every link now returns None instead of the
            # next uncaveated number.
            _mark_wow_comparability(
                out["wow"], _rolling_spans(7, 2),
                ("real_agents_pct", "real_calls_pct"), "rolling")
            # top tools among real traffic only
            rows = _reach_bounded(cur,
                "SELECT tool_name, COUNT(*) AS calls, "
                "       COUNT(DISTINCT agent_id) FILTER (WHERE " + _pub + ") AS agents "
                "FROM " + _from(_7d) + " WHERE (" + _real + ") AND tool_name IS NOT NULL "
                "GROUP BY tool_name ORDER BY calls DESC LIMIT 12")
            out["top_tools_7d"] = [
                {"tool": t, "calls": int(calls or 0), "agents": int(agents or 0)}
                for (t, calls, agents) in (rows or [])]
            # instrument-before-spend (2026-07-20): DEPTH-per-agent distribution.
            # The 2026-07 growth read named the floor as day-2 return (~6%), but
            # agent tasks are frequently one-shot — so 2nd-day return may be the
            # WRONG lever. Median + p90 calls per distinct agent_id (real+public,
            # canonical identity view) exposes whether agents go DEEP in a single
            # visit; if p90 is high the lever is depth-per-visit, not return.
            # Own bounded tx (same _reach_bounded envelope); additive field only.
            try:
                r = _reach_bounded(cur,
                    "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY c), "
                    "       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY c), "
                    "       AVG(c), MAX(c), COUNT(*) "
                    "FROM (SELECT agent_id, COUNT(*) AS c FROM " + _from(_7d) +
                    "      WHERE (" + _real + ") AND " + _pub +
                    "        AND agent_id IS NOT NULL "
                    "      GROUP BY agent_id) pa", fetch="one") or (None, None, None, None, 0)
                _med, _p90, _mean, _mx, _n = r
                out["depth_per_agent_7d"] = {
                    "agents":       int(_n or 0),
                    "median_calls": round(float(_med), 2) if _med is not None else None,
                    "p90_calls":    round(float(_p90), 2) if _p90 is not None else None,
                    "mean_calls":   round(float(_mean), 2) if _mean is not None else None,
                    "max_calls":    int(_mx or 0),
                }
            except Exception as e:
                out["flags"]["depth_per_agent_error"] = str(e)[:160]
        out["flags"]["calls_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["calls_error"] = str(e)[:160]
    # 30d retention — straight read of the canonical retention view.
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            r = _reach_bounded(cur,
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE returned_2nd_day) "
                "FROM mcp_agent_retention_30d", fetch="one") or (0, 0)
            n30, ret = int(r[0] or 0), int(r[1] or 0)
            out["retention_30d"] = {
                "agents": n30,
                "returned_2nd_day": ret,
                "day2_return_rate_pct": round(100.0 * ret / n30, 1) if n30 else None,
            }
        out["flags"]["retention_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["retention_error"] = str(e)[:160]
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            r = _reach_bounded(cur,
                "SELECT COUNT(*), COUNT(DISTINCT engine) FROM ai_citations "
                "WHERE observed_at >= NOW() - (7 * INTERVAL '1 day') AND dchub_cited = TRUE",
                fetch="one") or (0, 0)
            out["citations_7d"] = int(r[0] or 0)
            out["citation_engines_7d"] = int(r[1] or 0)
        out["flags"]["citations_available"] = True
        out["data_available"] = True
    except Exception as e:
        out["flags"]["citations_error"] = str(e)[:160]
    return out


_REACH_CACHE: dict = {"data": None, "ts": 0.0}
_REACH_CACHE_TTL_S = 300   # reach numbers move on hour scales; 5 min is plenty fresh
_REACH_REFRESH_LOCK = threading.Lock()   # single-flight guard for the bg rebuild
_REACH_REFRESH_RUNNING = False


def _reach_refresh_cache():
    """Rebuild the reach payload and publish it. Runs in a daemon thread."""
    global _REACH_REFRESH_RUNNING
    try:
        data = _reach_build_data()
        _REACH_CACHE["data"] = data
        _REACH_CACHE["ts"] = time.time()
    except Exception as e:  # _reach_build_data is defensive, but never kill the flag
        import logging as _lg
        _lg.getLogger(__name__).warning("reach background refresh failed: %s", e)
    finally:
        with _REACH_REFRESH_LOCK:
            _REACH_REFRESH_RUNNING = False


@mcp_bp.get("/api/v1/reach")
def reach_dashboard_data():
    """Serve the reach payload from cache; refresh in the background when stale.

    Latency fix (2026-07-02): during deploy/DB-contention windows the six
    aggregate scans ran 43-55s (Railway HTTP logs, deployment 322e21fe) —
    past the CF Pages worker's 5s attempt / 15s retry budget, so reach.html
    rendered "could not load /api/v1/reach" even though the endpoint was
    healthy minutes later. Same stale-while-revalidate shape as
    routes/funnel_health._data_cached: fresh → serve; stale → serve
    instantly + ONE daemon thread (single-flight) rebuilds off the request
    path; only the first request after boot builds inline, bounded by the
    4s per-query SET LOCAL statement_timeout in _reach_bounded."""
    global _REACH_REFRESH_RUNNING
    now = time.time()
    data = _REACH_CACHE["data"]
    if data is not None:
        age = now - _REACH_CACHE["ts"]
        if age >= _REACH_CACHE_TTL_S:
            # Stale: serve it now, rebuild in the background (single-flight).
            with _REACH_REFRESH_LOCK:
                if not _REACH_REFRESH_RUNNING:
                    _REACH_REFRESH_RUNNING = True
                    threading.Thread(target=_reach_refresh_cache,
                                     name="reach-refresh",
                                     daemon=True).start()
        payload = dict(data)   # shallow copy so the annotation never races the cache
        payload["cache_age_s"] = round(age, 1)
        return jsonify(payload), 200
    # First build since boot — no stale copy to serve, run inline.
    data = _reach_build_data()
    _REACH_CACHE["data"] = data
    _REACH_CACHE["ts"] = time.time()
    return jsonify(data), 200


# ── POST /api/v1/keys/claim/quote — OPT-IN testimonial capture ─────────────
# Master-shell 2 (2026-06-02): the honest capture path. ~90% of traffic is
# anonymous LLM-proxy with no identity; the only place a real human/agent
# can volunteer a quote is a real-identity touchpoint (they hold a claimed
# key). This endpoint is STRICTLY OPT-IN — an agent only calls it if its
# operator chose to share a quote.
#
# Stored to ai_testimonials with source='claim_quote' and approved=FALSE
# (manual admin approval before anything is shown publicly). No email / PII
# is written here — ai_testimonials has no email column, and we deliberately
# do NOT copy the key's email into it. The public testimonial shows only the
# name + company + quote the user chose to share.

@mcp_bp.post("/api/v1/keys/claim/quote")
def claim_key_quote():
    """Public, OPT-IN. Attach a volunteered quote to a claimed key.

    Body: {"api_key": "dch_live_...", "quote": "...",
           "name": "Jane Doe" (optional), "company": "Acme" (optional)}

    The quote is stored UNAPPROVED (approved=FALSE) for manual admin review.
    Never auto-published, never exposes the key's email.
    """
    body = request.get_json(silent=True) or {}
    api_key = (str(body.get("api_key") or "")).strip()
    quote = (str(body.get("quote") or "")).strip()
    # Public-safe identity the user CHOSE to share. Never an email.
    name = (str(body.get("name") or "")).strip()[:120]
    company = (str(body.get("company") or "")).strip()[:160]

    if not api_key:
        return jsonify(ok=False, error="missing_api_key",
                       message="Pass the api_key you claimed from /api/v1/keys/claim."), 200
    if not quote or len(quote) < 15:
        return jsonify(ok=False, error="quote_too_short",
                       message="Share a sentence or two about how DC Hub helped (min 15 chars)."), 200
    quote = quote[:1500]
    # Guard: if someone pastes an email into the quote/name, redact it so we
    # never store PII even by user error (capture must not expose PII).
    _email_pat = _kc_re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
    quote = _email_pat.sub("[redacted]", quote)
    name = _email_pat.sub("", name).strip()

    # Resolve the key → confirm it exists + pick up its real platform (if the
    # claim recorded one) so the captured quote is attributed honestly.
    platform = "mcp_agent"
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, metadata FROM mcp_dev_keys WHERE api_key = %s",
                (api_key,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="unknown_api_key",
                               message="That key isn't recognized. Claim one at /api/v1/keys/claim."), 200
            status = row[0]
            if status and status != "active":
                return jsonify(ok=False, error="key_inactive",
                               message=f"That key is {status}."), 200
            meta = row[1] or {}
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            _cn = (meta.get("client_name") or "").strip().lower()
            if _cn and _cn not in _LIVE_PROOF_NONPLATFORM:
                platform = _cn[:80]
    except Exception as e:
        return jsonify(ok=False, error="lookup_failed",
                       message="Couldn't verify that key right now — your key still works; try again later.",
                       detail=str(e)[:120]), 200

    # Store UNAPPROVED. agent_name <- user-chosen name; context <- company.
    # source='claim_quote'; approved defaults FALSE (manual admin approval).
    try:
        _ensure_testimonial_quote_schema()
        with _pool.connection() as conn, conn.cursor() as cur:
            # Exact-duplicate guard (same platform + same TEXT): a retried
            # POST is acknowledged idempotently instead of stacking pending
            # rows. Deliberately NOT company-scoped — the old broad UNIQUE
            # (platform, context) constraint capped capture at one quote per
            # company and turned every later one into storage_failed.
            cur.execute(
                """SELECT id FROM ai_testimonials
                    WHERE source = 'claim_quote' AND platform = %s
                      AND quote = %s LIMIT 1""",
                (platform, quote),
            )
            _dup = cur.fetchone()
            if _dup:
                return jsonify(
                    ok=True, captured=True, already_captured=True,
                    id=_dup[0], approved=False,
                    message=("Already had that exact note — it's pending review. "
                             "A different quote is welcome any time."),
                ), 200
            # Bare TARGET-LESS ON CONFLICT DO NOTHING (house ingest-idempotency
            # lint); inert for claim_quote rows — see the identify-path twin.
            cur.execute(
                """INSERT INTO ai_testimonials
                       (platform, agent_name, quote, context, category, source, approved)
                   VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                (platform, (name or None), quote, (company or None),
                 "recommendation", "claim_quote"),
            )
            new_id = (cur.fetchone() or [None])[0]
    except Exception as e:
        note_swallowed_write("ai_testimonials",
                             where="flask_mcp_endpoints.claim_key_quote")
        return jsonify(ok=False, error="storage_failed",
                       message="Couldn't save that right now — try again later.",
                       detail=str(e)[:120]), 200

    return jsonify(
        ok=True,
        captured=True,
        id=new_id,
        approved=False,
        message=("Thank you — your note was received and is pending review. "
                 "Nothing is published until a human approves it, and we never "
                 "share your email."),
    ), 200
