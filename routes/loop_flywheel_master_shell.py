"""
routes/loop_flywheel_master_shell.py — Loop & Flywheel Master Shell (#29, 2026-07-25).

The CROSS-DOMAIN board. Every other shell watches one subsystem; this one
watches the seams between them — the places where a healthy-looking part sits
next to a broken neighbour and nobody owns the join.

Nine lanes, one per domain of the 07-25 loop review:

  1. INFRA (Railway/Neon)   — replica routing + the CALENDAR-CRITICAL Neon
     Azure→AWS migration (due 2026-10-05; a date, not a metric — it goes red
     on its own as the deadline nears, because nothing else will remind us).
  2. EDGE (Cloudflare)      — admin/API surfaces that must never be edge-cached.
     A cached admin GET served a 30-min-stale board on 07-25 and read as a
     failed deploy; this lane keeps that class visible.
  3. FAILOVER (Render)      — mirror freshness. NOTE: the shell does NOT want
     a deploy-per-push; brain_autopilot already self-heals >2h drift on a
     30-min cooldown, and Render pipeline minutes are the scarce resource.
     This lane MEASURES lag and names that autopilot action as the actuator.
  4. IDENTITY (licenses)    — the flywheel's #1 leak: post-claim session carry
     and claimed-key activation. Wave 3 shipped the re-stamp fix; this lane
     watches the two rates that prove it.
  5. RAG                    — corpus breadth (wave-3 expansion) + gate registry.
  6. MCP                    — manifest drift across the three manifests.
  7. AI DOORS               — owed doors OPEN but unused: reach without calls
     is a distribution problem, and this states it in one number.
  8. INVENTORY              — published-vs-queue counts stated as FACTS (never
     a ratio: they are separate pipelines) + the supply-limited backlog rule.
  9. CRON                   — dead-man board health + the duplicate-job census
     from the 07-25 inventory (~314 live jobs, documented overlaps).

★ HONESTY RULE (inherited from Integrity #25): a lane must never read PASS
when it couldn't check — an indeterminate critical check renders "?" and the
lane is not green. Known-but-unfixed work renders FAIL, never green-by-silence.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/loop-flywheel/master-tick   JSON scoreboard (9 lanes)
  GET      /admin/loop-flywheel                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/loop-flywheel                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY).
Cron: cron_heartbeat `loop_flywheel_shell_daily` (08:xx UTC).
Kill: LOOP_FLYWHEEL_SHELL_DISABLE=1
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.request
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

loop_flywheel_master_shell_bp = Blueprint("loop_flywheel_master_shell", __name__)

# Neon Azure→AWS migration deadline. A calendar fact, not a metric — the only
# honest way to watch it is to count down and go red before it bites.
_NEON_MIGRATION_DUE = datetime.date(2026, 10, 5)
_NEON_WARN_DAYS = 45        # amber inside this window, red inside half of it

# Flywheel identity thresholds (mirror routes/flywheel_master_shell.py so this
# shell never imports a sibling shell — kept literal on purpose).
_CARRY_FLOOR_PCT = 70.0
_ACTIVATION_FLOOR_PCT = 40.0

# (an inventory "verified/tracked ratio" floor lived here until the first live
# tick proved the two tables are separate pipelines, not a ratio — see
# _lane_inventory. Deliberately not replaced with another invented number.)
_INVENTORY_QUEUE_MAX_QUIET_DAYS = 7

# Admin/API path prefixes that must never be edge-cached.
_NO_CACHE_PATHS = ("/api/v1/admin/", "/admin/")

# Ledger statuses meaning "this loop is fine" — MUST mirror
# routes/ingest_runs._OK_STATUS (kept literal so this shell never imports a
# route module). no_new_data is the affirmative healthy-idle status.
_OK_STATUS = {"success", "ok", "idle", "no-op", "noop", "skipped", "",
              "no_new_data", "no-new-data"}
_NO_NEW_DATA = {"no_new_data", "no-new-data"}


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("LOOP_FLYWHEEL_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[loop-flywheel] db connect failed: %s", e)
        return None


def _row(c, sql: str):
    """Fail-soft single row. LITERAL SQL only — no params tuple and no percent
    characters anywhere (psycopg2 percent-substitution trap)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("[loop-flywheel] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """green ONLY when something was actually decided and nothing failed.

    2026-07-25 (adversarial review): this originally escalated to "?" only for
    checks marked critical, so a lane whose checks were ALL
    indeterminate-and-non-critical rendered a confident PASS — the exact
    green-by-silence the module header forbids. Observed live: with the DB
    unreachable, lane 7 returned [door_usage=None] and read PASS, and lane 3
    read PASS while "failover mirror answers" was never determined. Matches
    routes/integrity_master_shell.py:161 (#25), the reference implementation."""
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in checks if k.get("critical")):
        return "?"
    # nothing decided at all ⇒ nothing proven ⇒ never green
    if not [k for k in checks if k["pass"] is not None]:
        return "?"
    return "PASS"


def _as_dt(ts):
    """Coerce a DB timestamp into an aware datetime. Several house tables
    store timestamps as TEXT (coverage_gaps.created_at 500'd growthfix's first
    live tick), so strings are parsed, never assumed. None on failure."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00").strip())
        except Exception:  # noqa: BLE001
            return None
    if getattr(ts, "tzinfo", None) is None:
        try:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    return ts


def _age_days(ts) -> float | None:
    ts = _as_dt(ts)
    if ts is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def _http_head(url: str, timeout: float = 6.0, headers: dict | None = None):
    """Return (status, headers) or (None, {}). Never raises. An HTTP error
    status is still an ANSWER (401/404 carry headers), so it is returned
    rather than swallowed as a failure."""
    try:
        h = {"User-Agent": "dchub-loop-flywheel-shell/1.0"}
        h.update(headers or {})
        req = urllib.request.Request(url, method="GET", headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
    except Exception:  # noqa: BLE001
        return None, {}


# ── lane 1: infra (Railway / Neon) ────────────────────────────────────

def _lane_infra() -> list[dict]:
    checks = []
    days = (_NEON_MIGRATION_DUE - datetime.date.today()).days
    # Deliberately hard: this is a deadline nobody else tracks. Amber at 45d,
    # red at 22d — early enough that a migration still fits in the window.
    if days < 0:
        ok, note = False, f"OVERDUE by {abs(days)}d"
    elif days <= _NEON_WARN_DAYS // 2:
        ok, note = False, f"{days}d left — schedule the cutover NOW"
    elif days <= _NEON_WARN_DAYS:
        ok, note = None, f"{days}d left — inside the planning window"
    else:
        ok, note = True, f"{days}d out"
    checks.append(_check(
        "neon_migration", "Neon Azure→AWS migration on schedule", ok,
        f"due {_NEON_MIGRATION_DUE.isoformat()} · {note}", critical=True))
    replica = bool((os.environ.get("NEON_REPLICA_URL") or "").strip())
    checks.append(_check(
        "read_replica", "read replica configured for heavy reads", replica,
        ("NEON_REPLICA_URL set — route sitemap/export reads via get_read_db"
         if replica else "NEON_REPLICA_URL unset: heavy reads hit the primary "
                         "pool (sitemap-shard stampede class)")))
    role = (os.environ.get("DCHUB_ROLE") or "").strip() or "unset"
    checks.append(_check(
        "role_split", "role split declared", role != "unset",
        f"DCHUB_ROLE={role} (heartbeat/brain belong to WORKER, not WEB)"))
    return checks


# ── lane 2: edge (Cloudflare) ─────────────────────────────────────────

def _lane_edge() -> list[dict]:
    checks = []
    # Probe LOOPBACK, not the public host: from inside Railway the egress to
    # dchub.cloud is unreliable (first live tick rendered "probe failed
    # (network)" every time, which checks nothing). Loopback proves the
    # APP sets no-store — the half we control. Whether the EDGE honors it
    # needs an off-box probe, so that half stays honestly indeterminate.
    port = os.environ.get("PORT", "8080")
    akey = (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    st, h = _http_head(f"http://127.0.0.1:{port}"
                       "/api/v1/admin/brain-ascension/master-tick",
                       headers={"X-Admin-Key": akey} if akey else None)
    cc = (h.get("cache-control") or "").lower()
    if st is None:
        checks.append(_check("admin_nocache", "admin responses declare no-store",
                             None, "loopback probe failed", critical=True))
    elif st != 200:
        # Without a 200 we never saw the real response headers — say so
        # rather than pass on a 401 that legitimately carries no cache header.
        checks.append(_check(
            "admin_nocache", "admin responses declare no-store", None,
            f"probe got HTTP {st} (need 200 to read the real headers)",
            critical=True))
    else:
        checks.append(_check(
            "admin_nocache", "admin responses declare no-store",
            "no-store" in cc,
            f"HTTP 200 cache-control='{cc or '(none)'}'"
            + ("" if "no-store" in cc else " — MISSING no-store"),
            critical=True))
    checks.append(_check(
        "nocache_policy", "no-cache policy documented for admin prefixes", True,
        "prefixes: " + ", ".join(_NO_CACHE_PATHS)
        + " — a cached admin GET reads as a failed deploy (07-25 incident)"))
    return checks


# ── lane 3: failover (Render mirror) ──────────────────────────────────

def _lane_failover() -> list[dict]:
    checks = []
    st, h = _http_head("https://dchub-backend-render.onrender.com/health")
    checks.append(_check(
        "render_up", "failover mirror answers", (st == 200) if st else None,
        (f"HTTP {st}" if st else "unreachable (cold start or down)")))
    hook = bool((os.environ.get("RENDER_DEPLOY_HOOK_URL")
                 or os.environ.get("RENDER_DEPLOY_HOOK") or "").strip())
    checks.append(_check(
        "render_selfheal", "drift self-heal actuator configured", hook,
        ("RENDER_DEPLOY_HOOK_URL set — brain_autopilot fires it when the "
         "mirror is >2h behind (30-min cooldown). Deliberately NOT per-push: "
         "commit churn burns Render pipeline minutes."
         if hook else "hook env unset — autopilot can only escalate, not heal"),
        critical=True))
    return checks


# ── lane 4: identity / licenses (the flywheel's #1 leak) ──────────────

def _lane_identity(c) -> list[dict]:
    checks = []
    # Wave-3 fix present? (re-stamp on reuse — the code-level guarantee)
    try:
        import flask_mcp_endpoints as _fme
        wired = hasattr(_fme, "_restamp_claim_session")
    except Exception:  # noqa: BLE001
        wired = None
    checks.append(_check(
        "restamp_wired", "reused claims re-stamp the live session", wired,
        ("_restamp_claim_session present — reuse branches re-point the key at "
         "the claiming session" if wired else
         "re-stamp helper MISSING: reused keys keep a stale session_id and "
         "drop out of the carry denominator"),
        critical=True))
    carry = _row(c, """
        WITH claims AS (
          SELECT api_key, created_at, metadata->>'session_id' AS sid
            FROM mcp_dev_keys
           WHERE api_key LIKE 'dch_live_' || '%'
             AND metadata->>'source' = 'claim_api'
             AND metadata->>'session_id' IS NOT NULL
             AND metadata->>'session_id' NOT IN ('None', '')
             AND created_at >= NOW() - INTERVAL '30 days')
        SELECT
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM mcp_call_log l
             WHERE l.session_id = c.sid AND l.timestamp > c.created_at)),
          COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM mcp_call_log l
             WHERE l.session_id = c.sid AND l.timestamp > c.created_at
               AND l.api_key = c.api_key))
          FROM claims c""") if c else None
    if not carry or not carry[0]:
        checks.append(_check("carry_rate", "post-claim sessions carry the key",
                             None, "no claim cohort in window / unreadable",
                             critical=True))
    else:
        kept, carried = int(carry[0] or 0), int(carry[1] or 0)
        pct = 100.0 * carried / max(1, kept)
        checks.append(_check(
            "carry_rate", "post-claim sessions carry the key",
            pct >= _CARRY_FLOOR_PCT,
            f"{carried}/{kept} = {pct:.1f}% (floor {_CARRY_FLOOR_PCT:.0f}%) — "
            "the activation wiring leak; wave-3 re-stamp should lift this",
            critical=True))
    act = _row(c, """
        WITH claimed AS (
          SELECT api_key, created_at FROM mcp_dev_keys
           WHERE api_key LIKE 'dch_live_' || '%'
             AND metadata->>'source' = 'claim_api'
             AND created_at >= NOW() - INTERVAL '30 days'
             AND created_at <  NOW() - INTERVAL '7 days')
        SELECT COUNT(*), COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM mcp_call_log l
             WHERE l.api_key = c.api_key
               AND l.timestamp > c.created_at + INTERVAL '1 minute'))
          FROM claimed c""") if c else None
    if not act or not act[0]:
        checks.append(_check("activation", "claimed keys used after mint", None,
                             "no mature cohort / unreadable"))
    else:
        tot, used = int(act[0] or 0), int(act[1] or 0)
        pct = 100.0 * used / max(1, tot)
        checks.append(_check(
            "activation", "claimed keys used after mint",
            pct >= _ACTIVATION_FLOOR_PCT,
            f"{used}/{tot} = {pct:.1f}% (floor {_ACTIVATION_FLOOR_PCT:.0f}%) — "
            "next lever: machine-actionable key echo in the claim payload"))
    return checks


# ── lane 5: rag ───────────────────────────────────────────────────────

def _lane_rag() -> list[dict]:
    checks = []
    try:
        from routes.brain_rag import CORPORA, PUBLIC_CORPORA, PROVIDER_COSINE_GATES
        # wave-3 expansion: the prose shelves that were unindexed on 07-25
        want = ("press_releases", "announcements", "permitting_intel",
                "construction_permits", "tax_incentives_neon", "capacity_pipeline")
        missing = [w for w in want if w not in CORPORA]
        checks.append(_check(
            "corpus_breadth", "wave-3 prose corpora registered",
            len(missing) == 0,
            (f"{len(CORPORA)} corpora registered, {len(PUBLIC_CORPORA)} public"
             if not missing else "MISSING: " + ", ".join(missing)),
            critical=True))
        prov = (os.environ.get("RAG_EMBED_PROVIDER") or "mistral").strip().lower()
        checks.append(_check(
            "gate_registry", "cosine gates registered for live provider",
            prov in PROVIDER_COSINE_GATES,
            f"provider={prov} registered={prov in PROVIDER_COSINE_GATES}"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("corpus_breadth", "wave-3 prose corpora registered",
                             None, f"brain_rag probe failed: {type(e).__name__}",
                             critical=True))
    return checks


# ── lane 6: mcp manifests ─────────────────────────────────────────────

def _lane_mcp() -> list[dict]:
    checks = []
    st, _ = _http_head("https://dchub.cloud/api/v1/mcp/tools.json")
    checks.append(_check(
        "tools_manifest", "live tool manifest served",
        (st == 200) if st else None,
        (f"HTTP {st}" if st else "unreachable"), critical=True))
    checks.append(_check(
        "manifest_sot", "manifest source-of-truth documented", True,
        "SoT = dchub-mcp-server/server.mjs; three manifests historically "
        "disagreed — sync --fix belongs in CI, not in a human's memory"))
    return checks


# ── lane 7: ai doors (distribution, not engineering) ──────────────────

def _lane_ai_doors(c) -> list[dict]:
    checks = []
    # 2026-07-25 first live tick: this queried mcp_client, which is NOT a
    # column on mcp_call_log (it lives on mcp_upgrade_signals) — the lane
    # rendered "unreadable" every tick. The real column is `platform`.
    row = _row(c, """
        SELECT COUNT(DISTINCT platform), COUNT(*)
          FROM mcp_call_log
         WHERE timestamp >= NOW() - INTERVAL '7 days'""") if c else None
    if row is None:
        checks.append(_check("door_usage", "owed doors carry real agent calls",
                             None, "mcp_call_log unreadable"))
    else:
        clients, calls = int(row[0] or 0), int(row[1] or 0)
        checks.append(_check(
            "door_usage", "owed doors carry real agent calls",
            (clients >= 3) if calls else None,
            f"{clients} distinct clients / {calls} calls (7d) — reach without "
            "calls is a DISTRIBUTION gap (per-platform onboarding), not a "
            "missing door"))
    return checks


# ── lane 8: inventory (the moat's weakest public number) ──────────────

def _lane_inventory(c) -> list[dict]:
    """Inventory is REPORT-ONLY on the counts and CHECKS only what is truly
    checkable.

    2026-07-25, first live tick: this lane originally divided facilities by
    discovered_facilities and rendered "317.7%" as a PASS. That was a
    fabricated ratio — `facilities` (published/serving) and
    `discovered_facilities` (discovery queue) are two different pipelines,
    not numerator and denominator. A meaningless number that reads green is
    exactly what the honesty rule exists to prevent, so the ratio is gone.
    What remains: both counts as stated facts, plus one REAL check — that
    the discovery queue is still accruing."""
    checks = []
    # Published `facilities` is the INTENDED subject here, per this lane's
    # docstring: it reports the published count and the discovery-queue count
    # as two separate stated facts, not as a ratio. Switching to
    # discovered_facilities would report the same pipeline twice and delete
    # the comparison the lane exists to make.
    # lint: legacy-facilities-ok
    pub = _row(c, "SELECT COUNT(*) FROM facilities") if c else None
    disc = _row(c, "SELECT COUNT(*) FROM discovered_facilities "
                   "WHERE COALESCE(is_duplicate, 0) = 0") if c else None
    checks.append(_check(
        "counts", "published + discovery-queue counts", True,
        (f"facilities(published)={int(pub[0]) if pub else '?'} · "
         f"discovered_facilities(non-dupe queue)={int(disc[0]) if disc else '?'}"
         " — publish BOTH; they are separate pipelines, never a ratio")))
    fresh = _row(c, "SELECT MAX(last_updated) FROM discovered_facilities") if c else None
    age = _age_days(fresh[0]) if fresh and fresh[0] else None
    checks.append(_check(
        "queue_accruing", "discovery queue still accruing",
        (True if (age is not None and age <= _INVENTORY_QUEUE_MAX_QUIET_DAYS) else None),
        (f"newest discovered row {age:.1f}d ago" if age is not None
         else "unreadable / no timestamped rows") +
        " — the backlog is SUPPLY-limited; NEVER schedule a dedup to 'fix' it",
        critical=True))
    return checks


# ── lane 9: cron / dead-man ───────────────────────────────────────────

def _lane_cron(c) -> list[dict]:
    checks = []
    rows = None
    try:
        if c is not None:
            with c.cursor() as cur:
                cur.execute("SELECT feed, last_run, last_status, cadence_hours, "
                            "consecutive_zero, max_content_date FROM ingest_runs")
                rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        rows = None
    if rows is None:
        checks.append(_check("deadman", "dead-man board clear", None,
                             "ingest_runs unreadable", critical=True))
    else:
        # All FOUR canonical dead-man conditions (this originally checked only
        # consecutive_zero, so a feed that had gone silent, started reporting a
        # bad status, or emitted future-dated content still read clear).
        # Mirrors routes/growthfix_master_shell.py::_lane_ingest_board (#26).
        now = datetime.datetime.now(datetime.timezone.utc)
        # ★2026-09-02 (D2): LATE fails this lane; RED (ran on time, beat a
        # fault such as lanes_failing) is named as a note. Before the split
        # this lane failed because OTHER shells were red — a self-referential
        # cascade. Same line as routes/ingest_runs.deadman (_LATE_KINDS).
        late, reds = [], []
        for feed, lr, st, cad, cz, mcd in rows:
            cad_h = float(cad) if cad is not None else 48.0
            stl = str(st or "").lower()
            why_late, why_red = [], []
            lrz = _as_dt(lr)
            if lrz is None:
                why_late.append("never ran")
            elif (now - lrz).total_seconds() / 3600.0 > 2 * cad_h:
                why_late.append("stale")
            if stl not in _OK_STATUS:
                why_red.append("status=" + (stl or "(none)"))
            if (cz or 0) >= 3 and stl not in _NO_NEW_DATA:
                why_red.append(f"{cz} zero-row runs")
            mz = _as_dt(mcd)
            if mz is not None and mz > now + datetime.timedelta(hours=6):
                why_red.append("future content date")
            if why_late:
                late.append(f"{feed}({','.join(why_late + why_red)})")
            elif why_red:
                reds.append(f"{feed}({','.join(why_red)})")
        detail = (f"{len(rows)} feeds, {len(late)} overdue"
                  + (": " + "; ".join(late[:6]) if late else ""))
        if reds:
            detail += (f" · {len(reds)} red but ON TIME (their own lanes, not "
                       f"a cadence fault): " + "; ".join(reds[:6]))
        checks.append(_check(
            "deadman", "dead-man board clear", len(late) == 0, detail,
            critical=True))
    # Census is a standing FAIL until the dedup wave verifies-and-removes.
    checks.append(_check(
        "cron_dupes", "duplicate scheduled work retired", False,
        "WAVE 4: 07-25 inventory found ~314 live jobs with documented "
        "overlaps — infra-sync x5, news x4, backup x3, health probes x9, "
        "MCP registry x6, plus an ORPHANED dchub-scheduler.py (33 active + 48 "
        "disabled) nothing launches. Verify-then-remove, one family per pass."))
    return checks


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        body = json.dumps({
            "feed": "loop-flywheel-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-loop-flywheel-shell/1.0",
                     "X-Admin-Key": admin_key})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001
        logger.debug("[loop-flywheel] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       f"lane crashed: {type(e).__name__}: {str(e)[:120]}",
                       critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    c = _conn()
    try:
        lanes = [
            {"id": "infra", "name": "1 · infra (Railway/Neon)",
             "checks": _safe_lane(_lane_infra)},
            {"id": "edge", "name": "2 · edge (Cloudflare)",
             "checks": _safe_lane(_lane_edge)},
            {"id": "failover", "name": "3 · failover (Render mirror)",
             "checks": _safe_lane(_lane_failover)},
            {"id": "identity", "name": "4 · identity / licenses",
             "checks": _safe_lane(_lane_identity, c)},
            {"id": "rag", "name": "5 · rag corpus + gates",
             "checks": _safe_lane(_lane_rag)},
            {"id": "mcp", "name": "6 · mcp manifests",
             "checks": _safe_lane(_lane_mcp)},
            {"id": "ai_doors", "name": "7 · ai doors (distribution)",
             "checks": _safe_lane(_lane_ai_doors, c)},
            {"id": "inventory", "name": "8 · inventory (verified/tracked)",
             "checks": _safe_lane(_lane_inventory, c)},
            {"id": "cron", "name": "9 · cron / dead-man",
             "checks": _safe_lane(_lane_cron, c)},
        ]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes)
    out = {
        "ok": True,
        "shell": "loop-flywheel-29",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@loop_flywheel_master_shell_bp.route(
    "/api/v1/admin/loop-flywheel/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="LOOP_FLYWHEEL_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_run_tick(beat=(request.method == "POST")))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@loop_flywheel_master_shell_bp.route("/admin/loop-flywheel", methods=["GET"])
@loop_flywheel_master_shell_bp.route("/api/v1/admin/loop-flywheel",
                                     methods=["GET"])
def dashboard():
    if _disabled():
        return Response("loop-flywheel shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required (?admin_key=)", status=401,
                        mimetype="text/plain")
    d = _run_tick(beat=False)
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        rows.append(
            f"<tr><td class='lane'>{_esc(ln['name'])}</td>"
            f"<td style='color:{color.get(ln['verdict'], '#eab308')}'>"
            f"<b>{_esc(ln['verdict'])}</b></td><td>"
            + "<br>".join(
                ("&#9989; " if k["pass"] is True else
                 ("&#10060; " if k["pass"] is False else "&#10068; "))
                + _esc(k["name"]) + " — <span class='d'>" + _esc(k["detail"])
                + "</span>" for k in ln["checks"])
            + "</td></tr>")
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Loop &amp; Flywheel Shell #29</title>"
        "<style>body{background:#0b1020;color:#e2e8f0;font:14px/1.5 "
        "-apple-system,Segoe UI,sans-serif;margin:2rem}table{border-collapse:"
        "collapse;width:100%;max-width:1180px}td{border-bottom:1px solid "
        "#1e293b;padding:.6rem .8rem;vertical-align:top}.lane{white-space:"
        "nowrap;font-weight:600}.d{color:#94a3b8}h1{font-size:1.2rem}"
        "small{color:#64748b}</style>"
        "<h1>Loop &amp; Flywheel Master Shell #29</h1>"
        "<small>generated " + _esc(d["generated_at"]) + " · cross-domain · "
        "read-only · refreshes 60s · standing reds are WORK ORDERS "
        "(cron dedup wave 4) · kill LOOP_FLYWHEEL_SHELL_DISABLE=1</small>"
        "<table>" + "".join(rows) + "</table>")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register_loop_flywheel_master_shell(app):
    app.register_blueprint(loop_flywheel_master_shell_bp)
