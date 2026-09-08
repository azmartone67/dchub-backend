"""
routes/actuation_master_shell.py — Actuation Master Shell (#39, 2026-07-28).

★ THE SUBJECT OF THIS SHELL IS THE OTHER SHELLS.

Every master shell ends its own header with some version of "names an actuator per
lane, FIRES NOTHING" — Flywheel, Optimization Engines, Platform-Doors, the Agent
portal, all read-only. The brain's innovation dashboard shows 15 approved
self-agenda items whose footer reads "not a single-file edit; recorded only". So
the apparatus that measures DC Hub is excellent and the apparatus that CHANGES it
is empty. That is why the numbers do not move after six months of work: the loop
is open at the actuation step, and each new diagnostic shell widens the diagnosis
without narrowing the gap.

This shell measures THAT gap, on the five items the 2026-07-28 review isolated.
It is the one shell that fires an actuator (lane 3, POST-gated) rather than only
naming one — because a sixth read-only shell would BE the bug.

LANES
  1. HANDOFF DELIVERY — the 77→0 cliff. relay_opens contains exactly two rows and
     BOTH are our own traffic ('human-simulated/2.0', 'dchub-ops-verify/1.0'), so
     zero real humans have ever opened a relay link. The valid=t synthetic open
     PROVES the write path works, which narrows the 27-day-old open question
     (brain investigation #14) from "no handoffs OR broken write path" down to
     delivery/appeal alone. mcp_conversion_clicks has been dead since 2026-05-05.
  2. BRAIN EVIDENCE ASSEMBLY — 79 of 112 self-agenda drafts in 30d (70.5%) carry a
     refutation saying needed evidence was "not present in the evidence", and mean
     confidence sits at 0.44. The reasoner and the critic are both good.
     ★★ CAUSE CORRECTED 2026-07-28. The original text here blamed retrieval for
     "citing prior findings by id without inlining their content". That was WRONG,
     and it was wrong in the direction that makes a fix look cheap. Live rows show
     prior_work IS fully inlined as text (89/111 drafts) and prior_fixes in 32/111.
     The actual cause: gather_evidence() took NO ARGUMENTS, so 111 DISTINCT
     questions yielded only SEVEN evidence-source signatures (46 sharing one). A
     question about a 404 on /api/v1/energy/retail/rates was handed facility
     counts, ISO counts and funnel KPIs — nothing about that endpoint. The critics
     asked for exactly what was missing (21 of 111 named timestamps/recency).
     ★ A diagnosis nobody re-derived from the raw rows survived into the shell's
     own header AND its actuator text. Read the primary rows, not the summary.
  3. INVESTIGATION LANE DARK — brain_investigations' newest row is 2026-07-01 (27
     days). self_agenda and enhancement_proposals both ran today, so it is ONE dead
     lane, not a dead brain. Root cause: NOTHING schedules POST
     /api/v1/brain/investigate — main.py's own registration log says it "ships
     dark". All 9 existing investigations were hand-triggered.
  4. PLATFORM-ACTIVE HONESTY — /ai publishes "15 AI PLATFORMS CONNECTED" beside
     "across 6 AI platforms" on the same page, because the 15 counts any platform
     card with status='active' and that status is granted on as little as ONE
     lifetime request (Groq 1, HuggingFace 1, Cohere 5). ai_platform_canon fixed
     the reach count on 2026-07-27; this lane guards the separate lifetime counter.
  5. INTEGRATION→REVENUE GATE — 9 paid conversions in 30d, all of them
     web:pricing-page (6) or organic_no_mcp_touch (3). ZERO MCP-attributed, 0
     emails captured. This lane is the gate on more platform-integration work:
     15 platforms connected and 0 agent-attributed dollars means a 16th changes
     nothing until lane 1 closes.

★ Pure-DB (read replica). NO self-requests through the public edge (the 2026-07-06
pool-saturation footgun) — the ONE actuation goes to an internal cron path and only
on explicit POST. Fail-soft. Admin-gated. Snapshot to the PRIMARY.
Kill: ACTUATION_SHELL_DISABLE=1

Endpoints:
  GET/POST /api/v1/admin/actuation/master-tick   JSON (5 lanes)
      POST ?fire=investigations   ← fires lane 3's actuator (the dark investigator)
  GET      /admin/actuation                       HTML (60s refresh)
  GET      /api/v1/admin/actuation                CF zone-worker bypass alias
"""
from __future__ import annotations

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)

actuation_master_shell_bp = Blueprint("actuation_master_shell", __name__)

_TICK_TTL = 30
_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()

# User agents that are OUR OWN probes/simulations. A relay open from one of these
# proves the write path, never demand — counting them as humans is exactly the
# self-traffic inflation this shell exists to prevent.
_SYNTHETIC_UA = ("human-simulated", "dchub-ops-verify", "dchub-", "dchubhealer",
                 "brain-radar", "brain-v2-headless", "uptimerobot", "curl/",
                 "python-", "node-fetch", "value-harness", "render-verify")

_EVIDENCE_STARVED_RE = (
    "not (present |in )?(anywhere )?in the evidence"
    "|not in the evidence block"
    "|does not appear anywhere in the evidence"
    "|absent from the evidence"
    "|not present in the evidence")

_STARVED_MAX_PCT = 40.0      # above this, retrieval is the bottleneck
_CONF_MIN = 0.55             # mean draft confidence floor
_LANE_DARK_DAYS = 7          # a brain lane silent longer than this is dark
_ACTIVE_MIN_CALLS = 25       # lifetime calls before a platform may be "active"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("ACTUATION_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    """Read replica (this shell only reads). Falls back to primary."""
    try:
        import psycopg2 as _pg
        url = (os.environ.get("NEON_REPLICA_URL")
               or os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[actuation] db connect failed: %s", e)
        return None


def _rows(c, sql: str) -> list:
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[actuation] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _scalar(c, sql: str):
    r = _rows(c, sql)
    return (r[0][0] if r and r[0] else None)


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:340], "critical": critical}


def _lane_verdict(checks: list):
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


def _ua_not_synthetic_sql(col: str) -> str:
    """SQL predicate: this user_agent is NOT one of our own probes.

    ★Single % on purpose. _rows() calls cur.execute(sql) with NO params, so psycopg2
    performs no %-interpolation and a doubled %% would survive into the SQL literally
    (harmless here only because consecutive LIKE wildcards collapse — see the
    empty-tuple percent trap). Never pass this string to an execute() WITH params.
    """
    parts = " AND ".join(
        f"COALESCE({col},'') NOT ILIKE '%{m}%'" for m in _SYNTHETIC_UA)
    return f"({parts})"


# ── lane 1 · handoff delivery (the 77→0 cliff) ────────────────────────
def _lane_handoff(c) -> list:
    out = []
    real = _scalar(c, "SELECT COUNT(*) FROM relay_opens WHERE "
                      f"{_ua_not_synthetic_sql('user_agent')}")
    total = _scalar(c, "SELECT COUNT(*) FROM relay_opens")
    real = int(real or 0)
    total = int(total or 0)
    out.append(_check(
        "relay_real_opens", "relay link opened by a REAL human (ever)", real > 0,
        f"{real} real of {total} total relay_opens — every other row is our own "
        f"probe traffic (human-simulated / dchub-ops-verify). This is the 77→0 "
        f"cliff, stated exactly.", critical=True))

    # A valid synthetic open PROVES the write path. That single fact splits the
    # 27-day-old open question in half.
    wrote = _scalar(c, "SELECT COUNT(*) FROM relay_opens WHERE valid IS TRUE")
    out.append(_check(
        "relay_write_path", "relay-open WRITE path proven functional",
        int(wrote or 0) > 0,
        f"{int(wrote or 0)} relay_opens row(s) with valid=t — the recorder works, "
        f"so zero real opens is a DELIVERY/appeal problem, not an attribution bug. "
        f"This retires half of brain investigation #14 (2026-07-01)."))

    clicks_age = _scalar(c, "SELECT round(EXTRACT(EPOCH FROM (now()-MAX(clicked_at)))"
                            "/86400.0, 1) FROM mcp_conversion_clicks")
    out.append(_check(
        "conversion_clicks_fresh", "mcp_conversion_clicks fresh (<7d)",
        (clicks_age is not None and float(clicks_age) <= 7),
        f"newest conversion click {clicks_age}d ago"
        if clicks_age is not None else "no conversion clicks ever recorded"))

    hand = int(_scalar(c, "SELECT COUNT(*) FROM agent_bus_handoffs") or 0)
    out.append(_check(
        "agent_bus_handoffs", "agent→human handoffs recorded", hand > 0,
        f"{hand} rows in agent_bus_handoffs (all-time)"))
    return out


# ── lane 2 · brain evidence assembly (the starvation) ─────────────────
def _lane_evidence(c) -> list:
    out = []
    r = _rows(c, "WITH d AS (SELECT confidence, result_json::text AS t "
                 "FROM brain_self_agenda "
                 "WHERE created_at > now()-interval '30 days') "
                 "SELECT COUNT(*), COUNT(*) FILTER (WHERE t ~* '"
                 + _EVIDENCE_STARVED_RE + "'), round(AVG(confidence)::numeric,2) FROM d")
    if not r:
        out.append(_check("evidence_read", "brain draft corpus readable", None,
                          "brain_self_agenda unreadable"))
        return out
    n, starved, conf = int(r[0][0] or 0), int(r[0][1] or 0), r[0][2]
    pct = (100.0 * starved / n) if n else 0.0
    out.append(_check(
        "evidence_starved", f"refutations citing missing evidence <= {_STARVED_MAX_PCT:.0f}%",
        pct <= _STARVED_MAX_PCT,
        f"{starved}/{n} drafts ({pct:.1f}%) in 30d were refuted because the evidence "
        f"block lacked what the question needed. ★CORRECTED 2026-07-28: this is NOT "
        f"'prior findings cited by id' — live rows show prior_work IS fully inlined "
        f"as text (89/111 drafts), prior_fixes in 32/111. The real cause is that "
        f"gather_evidence() took NO ARGUMENTS: 111 distinct questions produced only "
        f"SEVEN evidence-source signatures, so every question got the same generic "
        f"bundle. Fixed by gather_targeted_evidence(question).", critical=True))
    out.append(_check(
        "mean_confidence", f"mean draft confidence >= {_CONF_MIN}",
        (conf is not None and float(conf) >= _CONF_MIN),
        f"mean confidence {conf} across {n} drafts/30d — a direct consequence of "
        f"the starvation above, not of weak reasoning"))
    return out


# ── lane 3 · investigation lane dark ─────────────────────────────────
def _lane_investigations(c) -> list:
    out = []
    inv_age = _scalar(c, "SELECT round(EXTRACT(EPOCH FROM (now()-MAX(created_at)))"
                         "/86400.0,1) FROM brain_investigations")
    out.append(_check(
        "investigations_live", f"investigation lane ran within {_LANE_DARK_DAYS}d",
        (inv_age is not None and float(inv_age) <= _LANE_DARK_DAYS),
        f"newest brain_investigations row {inv_age}d ago. NOTHING schedules "
        f"POST /api/v1/brain/investigate — main.py's own registration log says it "
        f"'ships dark', so all existing investigations were hand-triggered.",
        critical=True))

    # The contrast check: prove it is ONE dead lane, not a dead brain.
    for tbl, label in (("brain_self_agenda", "self-agenda"),
                       ("brain_enhancement_proposals", "proposals")):
        age = _scalar(c, f"SELECT round(EXTRACT(EPOCH FROM (now()-MAX(created_at)))"
                         f"/86400.0,1) FROM {tbl}")
        out.append(_check(
            f"{tbl}_live", f"{label} lane still running (<2d)",
            (age is not None and float(age) <= 2),
            f"newest {tbl} row {age}d ago — this lane is HEALTHY, which is why the "
            f"dark investigator is a scheduling gap and not a brain failure"))
    return out


# ── lane 4 · platform-active honesty ─────────────────────────────────
def _lane_platform_honesty(c) -> list:
    out = []
    raw = _scalar(c, "SELECT COUNT(DISTINCT platform) FROM mcp_calls_identity "
                     "WHERE created_at > now()-interval '7 days' "
                     "  AND is_public_ip AND is_real_external AND platform IS NOT NULL")
    plats = [p[0] for p in _rows(c, "SELECT DISTINCT platform FROM mcp_calls_identity "
                                    "WHERE created_at > now()-interval '7 days' "
                                    "  AND is_public_ip AND is_real_external "
                                    "  AND platform IS NOT NULL")]
    canon = None
    try:
        from ai_platform_canon import count_platforms
        canon = count_platforms(plats)
    except Exception as e:
        logger.debug("[actuation] canon import failed: %s", e)
    out.append(_check(
        "canon_wired", "platform count flows through ai_platform_canon",
        canon is not None,
        f"canonical vendors={canon} vs {int(raw or 0)} raw distinct platform "
        f"strings (7d). The canon module collapses vendor aliases and drops "
        f"non-platforms (mcp, reviewer-sim, unknown)."))
    if canon is not None:
        out.append(_check(
            "canon_vs_raw", "no inflation between raw strings and canon vendors",
            int(raw or 0) <= canon,
            f"raw {int(raw or 0)} vs canon {canon} — any surface publishing the raw "
            f"number over-states platform breadth. /ai still shows a lifetime-ever "
            f"'active' count (15) beside the canon reach count on the same page."))

    thin = _rows(c, "SELECT platform, COUNT(*) AS n FROM mcp_calls_identity "
                    "WHERE is_public_ip AND is_real_external AND platform IS NOT NULL "
                    "GROUP BY 1 HAVING COUNT(*) < %d ORDER BY 2" % _ACTIVE_MIN_CALLS)
    out.append(_check(
        "thin_active", f"no platform called 'active' on < {_ACTIVE_MIN_CALLS} lifetime calls",
        len(thin) == 0,
        (f"{len(thin)} platform(s) below the floor: "
         + ", ".join(f"{p}={n}" for p, n in thin[:8])) if thin
        else "every counted platform clears the lifetime-call floor"))
    return out


# ── lane 5 · integration→revenue gate ────────────────────────────────
def _lane_revenue_gate(c) -> list:
    out = []
    rows = _rows(c, "SELECT COALESCE(source,'(null)'), COUNT(*) FROM mcp_conversions "
                    "WHERE created_at > now()-interval '30 days' "
                    "  AND COALESCE(is_test,false)=false GROUP BY 1 ORDER BY 2 DESC")
    total = sum(int(n or 0) for _, n in rows)
    mcp_attr = sum(int(n or 0) for s, n in rows
                   if s and "web" not in s.lower() and "organic" not in s.lower())
    out.append(_check(
        "mcp_attributed_revenue", "at least one MCP-attributed paid conversion (30d)",
        mcp_attr > 0,
        f"{mcp_attr} MCP-attributed of {total} total conversions/30d — "
        + (", ".join(f"{s}={n}" for s, n in rows) if rows else "none") +
        ". 15 platforms connected and zero agent-attributed dollars: a 16th "
        "integration changes nothing until lane 1 closes.", critical=True))

    # ★Label this precisely. It counts dev keys CREATED in 30d that carry an email —
    # NOT the weekly dividend's "emails captured (30d): 0", which counts a narrower
    # trial-key-email-bound population. Both can be true; conflating them would
    # manufacture exactly the label-vs-measure contradiction this shell polices.
    emails = _scalar(c, "SELECT COUNT(*) FROM mcp_dev_keys WHERE email IS NOT NULL "
                        "AND email <> '' AND created_at > now()-interval '30 days'")
    out.append(_check(
        "keys_with_email", "dev keys created in 30d carrying an email > 0",
        int(emails or 0) > 0,
        f"{int(emails or 0)} keys created in 30d have an email bound. NB the weekly "
        f"dividend reports 'emails captured (30d): 0' and 'trial keys email-bound: 1' "
        f"— a NARROWER population (trial keys only). Reconcile the two before either "
        f"number is published as 'emails captured'."))
    return out


_LANES = [
    ("handoff",   "1 · Handoff delivery (the 77→0 cliff)",      _lane_handoff,
     "FIRE the synthetic end-to-end handoff probe brain investigation #14 asked for "
     "on 2026-07-01 — but note lane 1 already proves the WRITE path, so the open "
     "question is now delivery/appeal only"),
    ("evidence",  "2 · Brain evidence assembly (starvation)",   _lane_evidence,
     "gather evidence about the SUBJECT of the question (targeted retrieval) before the "
     "drafting step — cheapest upgrade to brain output quality"),
    ("investig",  "3 · Investigation lane dark",                _lane_investigations,
     "★FIRES: POST /api/v1/admin/actuation/master-tick?fire=investigations kicks "
     "POST /api/v1/brain/investigate, which nothing currently schedules"),
    ("platform",  "4 · Platform-active honesty",                _lane_platform_honesty,
     "route every published platform COUNT through ai_platform_canon; retire the "
     "lifetime-ever 'active' counter on /ai"),
    ("revenue",   "5 · Integration→revenue gate",               _lane_revenue_gate,
     "GATE — hold new platform-integration work until an MCP-attributed conversion "
     "exists; the constraint is lane 1, not platform breadth"),
]


def _ensure_snapshots(pc) -> None:
    try:
        with pc.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS actuation_shell_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[actuation] snapshot ddl skipped: %s", e)


def _derive_question(lanes: list) -> str:
    """Build the investigation question from the CURRENTLY-failing critical checks.

    ★A cron that fires a frozen question forever becomes the stale noise this whole
    shell exists to catch: by the time it runs, the binding constraint may have
    moved. Deriving the question from live red criticals means the scheduled fire
    always asks about what is actually broken THIS week. Falls back to the relay
    cliff, which is the standing #1 while lane 1 is red.
    """
    reds = [ch["detail"] for lane in lanes for ch in lane["checks"]
            if ch.get("critical") and ch.get("pass") is False]
    if not reds:
        return ("All critical actuation checks are green for the first time. Which "
                "measurement is most likely wrong or too lenient, rather than the "
                "business genuinely being fixed?")
    body = " ".join(r.rstrip(".") + "." for r in reds[:3])
    return ("These are the measured, currently-failing critical constraints on DC "
            f"Hub's actuation loop: {body} Given these, what is the SINGLE highest-"
            "leverage change, and what would prove within 7 days that it worked?")


def _fire_investigations(lanes: list | None = None) -> dict:
    """Lane 3's actuator. The ONE thing this shell fires.

    Goes to the app's OWN blueprint via an internal HTTP call to the internal
    Railway URL — never through the public CF edge (the pool-saturation footgun).
    POST-gated and never called from the dashboard GET path.

    ★NB what is actually dark: brain-self-direct.yml already runs investigate()
    every 4h, but it writes the brain's OWN chosen questions to brain_self_agenda
    (live, 146 rows). brain_investigations holds questions POSED to it — that path
    has no scheduler, which is why it reads 26d stale. This actuator supplies the
    posed question, derived from live reds.
    """
    q = _derive_question(lanes or [])
    # ★.strip() is REQUIRED, not defensive: DCHUB_INTERNAL_API carries a TRAILING
    # NEWLINE in the Railway env. Without the strip, urllib rejects the URL outright
    # ("URL can't contain control characters … found at least '\\n'") — which is
    # exactly how this actuator failed on its first real fire (2026-07-28). The same
    # newline is already worked around at 12 separate call sites in
    # crawler_scheduler.py ("a trailing \\n … became %0a in the URL ->
    # NameResolutionError"), so treat any env-derived URL here as untrusted text.
    base = ((os.environ.get("DCHUB_INTERNAL_API") or "").strip()
            or (os.environ.get("RAILWAY_BACKEND_URL") or "").strip()
            or "http://127.0.0.1:" + ((os.environ.get("PORT") or "8080").strip()))
    if base and not base.startswith("http"):
        base = "https://" + base
    url = base.rstrip("/") + "/api/v1/brain/investigate"
    key = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

    # ★DISPATCH ON A THREAD, do not await. An investigation is an LLM call —
    # brain-self-direct.yml allows it 180s. The first attempt awaited it with a 20s
    # timeout and died with "The read operation timed out" while the work was still
    # running; awaiting it properly would instead pin a gunicorn worker for minutes
    # and starve the small pool (the same reason cron_reach_rollup threads its
    # heavy scan and returns 202). The investigation writes its own
    # brain_investigations row on completion, so fire-and-forget loses nothing.
    def _bg():
        try:
            import urllib.request
            req = urllib.request.Request(
                url, data=json.dumps({"question": q}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Admin-Key": key,
                         "User-Agent": "dchub-actuation-shell/1.0"},
                method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                logger.info("[actuation] investigate dispatched -> %s", r.status)
        except Exception as e:
            logger.warning("[actuation] investigate failed: %s", str(e)[:200])

    try:
        threading.Thread(target=_bg, daemon=True,
                         name="actuation-investigate").start()
        return {"dispatched": True, "url": url, "question": q[:400],
                "note": ("fire-and-forget: an investigation takes minutes and writes "
                         "its own brain_investigations row. Poll that table, or "
                         "/api/v1/brain/innovation/dashboard, for the result.")}
    except Exception as e:
        return {"dispatched": False, "error": str(e)[:300]}


def _run_tick(fired: dict | None = None) -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({"lane": key, "label": label, "pass": _lane_verdict(checks),
                      "actuator": actuator, "checks": checks, "ms": ms,
                      "fires": key == "investig",
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": ("DIAGNOSTIC + ONE LIVE ACTUATOR (lane 3, POST ?fire=investigations). "
                 "This shell's subject IS the other shells' 'fires nothing' problem."),
        "lanes_pass": sum(1 for l in lanes if l["pass"] is True),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "Actuation master shell #39 — routes/actuation_master_shell.py",
    }
    if fired is not None:
        payload["fired"] = fired
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    pc = None
    try:
        import psycopg2 as _pg
        purl = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if purl:
            pc = _pg.connect(purl, connect_timeout=8)
            pc.autocommit = True
            _ensure_snapshots(pc)
            with pc.cursor() as cur:
                cur.execute("INSERT INTO actuation_shell_snapshots "
                            "(lanes_pass, lanes_total, payload) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (payload["lanes_pass"], payload["lanes_total"],
                             json.dumps(payload)))
    except Exception as e:
        logger.debug("[actuation] snapshot insert skipped: %s", e)
    finally:
        if pc is not None:
            try:
                pc.close()
            except Exception:
                pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


@actuation_master_shell_bp.route("/api/v1/admin/actuation/master-tick",
                                methods=["GET", "POST"])
def actuation_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    if request.method == "POST" and (request.args.get("fire") or "") == "investigations":
        # Run the lanes FIRST so the question is derived from live reds, then
        # dispatch. ★Call this against the RAILWAY ORIGIN, not dchub.cloud: the CF
        # zone route has a 15s timeout that 503s admin POSTs (verified 2026-07-28 —
        # edge gave 503, origin gave 200), which is why every brain workflow uses
        # RAILWAY_BASE.
        payload = _run_tick()
        payload["fired"] = _fire_investigations(payload.get("lanes") or [])
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["payload"] = payload
        return jsonify(payload)
    return jsonify(_tick_cached())


@actuation_master_shell_bp.route("/admin/actuation", methods=["GET"])
@actuation_master_shell_bp.route("/api/v1/admin/actuation", methods=["GET"])
def actuation_dashboard():
    if _disabled():
        return Response("actuation shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;vertical-align:top'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px;vertical-align:top;white-space:nowrap'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] is True else ("#eab308" if lane["pass"] is None else "#ef4444")
        act_label = ("⚡ actuator (LIVE — this one fires): " if lane.get("fires")
                     else "⚡ actuator (not fired): ")
        act_color = "#a855f7" if lane.get("fires") else "#64748b"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:{act_color}'>{act_label}"
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='60'>"
        "<title>Actuation Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:980px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Actuation Master Shell "
        f"<span style='color:{'#22c55e' if green else '#ef4444'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px;line-height:1.5'>#39 · 07-28 · "
        f"<b>the subject of this shell is the other shells</b> — every one of them ends "
        f"'names an actuator per lane, fires nothing', which is why the numbers do not "
        f"move. Lane 3 <b style='color:#a855f7'>actually fires</b> "
        f"(POST ?fire=investigations). 30s cache · read replica · "
        f"generated {_esc(p['generated_at'])} · JSON "
        f"/api/v1/admin/actuation/master-tick</div>"
        + "".join(cards) +
        "<div style='color:#475569;font-size:11px;margin-top:16px'>A sixth read-only "
        "shell would BE the bug. Lanes 1, 2, 4 and 5 name actuators that are code "
        "changes or human decisions, not endpoint kicks — those are listed so they "
        "can be scheduled, not so they can be admired.</div></body>")
    return Response(html, mimetype="text/html")
