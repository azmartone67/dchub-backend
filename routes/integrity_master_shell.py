"""
routes/integrity_master_shell.py — Integrity Master Shell (#25, 2026-07-24).

Born from a morning spent chasing a regression that had not happened. The
operator read /admin/flywheel as "2/6 lanes green · 0 agents · 0 real calls
· X publisher dead 11d" and started triaging a collapse. The real flywheel
at that moment was 4/6 with 75 agents and an X post 0.8d old. The page had
been served by the RENDER failover origin, whose database is frozen at
2026-07-13 — and it answered HTTP 200.

That is the class of bug this shell exists to make impossible: an ops
surface that is CONFIDENTLY WRONG. Three lanes, each one a place where we
have now been burned by a measurement rather than by the thing measured.

  1. FAILOVER-ORIGIN INTEGRITY — is the mirror telling the truth? This is
     the ONLY lane here that cannot be answered from our own database: the
     mirror's staleness lives in a DIFFERENT database, so no query from the
     primary can ever see it. It needs one cross-origin probe.
  2. SLUG FREEZE — canonical_slug is frozen set-once, but NOTHING schedules
     the backfill (no cron references /api/v1/admin/slug/freeze), so newly
     ingested facilities accumulate unfrozen forever. 61 had piled up by
     2026-07-24. The check was already in the flywheel; the LOOP was never
     closed. Closed here by .github/workflows/slug-freeze-daily.yml.
  3. DCPI VERDICT SPREAD — the index had degenerated to AVOID=295 ·
     CAUTION=17 · BUILD=5 over 317 markets, and the self-heal meant to catch
     that guarded on (builds+avoids) >= 5, so it reported "verdicts
     adequate" forever. Downstream, the media arm can only ever write about
     five markets, which is why 4 of the last 20 press releases were
     Cheyenne.

★ MOSTLY-DB shell. Lanes 2 and 3 are pure Neon reads. Lane 1 makes exactly
ONE outbound request, to the OTHER origin's cheap public freshness probe,
with a hard 4s timeout. This is NOT the 2026-07-06 self-request footgun
that amplified pool saturation into 502s: that incident was this origin
fetching ITSELF through the public edge, re-entering the same Flask worker.
Here the target is a different service on a different host, is never
dchub.cloud, and is skipped outright if it resolves to this same origin
(_probe_target returns None). Lane 1 degrades to "?" rather than blocking.

READ-ONLY / DIAGNOSTIC: every lane names an actuator and fires NOTHING.

Endpoints:
  GET/POST /api/v1/admin/integrity/master-tick   JSON scoreboard (3 lanes)
  GET      /admin/integrity                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/integrity                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as flywheel_master_shell.
Kill: INTEGRITY_SHELL_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

integrity_master_shell_bp = Blueprint("integrity_master_shell", __name__)

# The mirror we probe in lane 1. Kept as a constant (not derived from a
# request header) so a spoofed Host can never redirect the probe.
_RENDER_ORIGIN = "https://dchub-backend-render.onrender.com"
_RAILWAY_ORIGIN = "https://dchub-backend-production.up.railway.app"


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("INTEGRITY_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (mirror flywheel_master_shell) ─────────────────────────

def _conn():
    """Raw psycopg2 connection. None on failure. Deliberately OUTSIDE the
    app pool — one short-lived connection per tick, so a tick never checks
    out a shared pool slot."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[integrity] db connect failed: %s", e)
        return None


def _scalar(c, sql: str):
    """Fail-soft scalar. None on error (NOT 0 — a probe must tell 'query
    broke' from 'count is zero'). Literal SQL only, NO params tuple: a
    literal % anywhere in the statement makes psycopg2 attempt
    %-substitution against an empty tuple and 500."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("[integrity] scalar failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _row(c, sql: str):
    """Fail-soft single row. None on error."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[integrity] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str, ms: int = 0,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate/gauge, shown as "?").

    critical=True means this check is the REASON the lane exists. If a
    critical check comes back undetermined, the lane must NOT render green —
    see _lane_verdict. Shipping a lane that reads PASS while admitting it
    could not reach the thing it audits would reproduce, inside this very
    shell, the failure it was built to catch."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:300], "ms": ms, "critical": critical}


def _lane_verdict(checks: list[dict]):
    """True / False / None for a lane.

    None ("?") when the lane could not establish its load-bearing fact —
    distinct from False ("something is broken") and from True. A dead mirror
    cannot serve confident zeros, so an unreachable probe is not a failure;
    but it is emphatically not a pass either."""
    if any(ch["pass"] is None and ch.get("critical") for ch in checks):
        return None
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


# ── lane 1 · failover-origin integrity ────────────────────────────────

def _probe_target() -> str | None:
    """The OTHER origin, or None when we cannot safely probe.

    Guard rails, in order:
      · explicit override wins (INTEGRITY_MIRROR_URL)
      · never probe dchub.cloud — that is the public edge, and hitting it
        would route back into THIS Flask process (the 07-06 incident)
      · never probe ourselves — if the target host equals the request host
        we return None and the lane degrades to '?'
    """
    override = (os.environ.get("INTEGRITY_MIRROR_URL") or "").strip()
    try:
        from routes.failover_stale_gate import _is_failover
        here_is_failover = _is_failover()
    except Exception:
        here_is_failover = False

    target = override or (_RAILWAY_ORIGIN if here_is_failover else _RENDER_ORIGIN)
    try:
        thost = (urlparse(target).hostname or "").lower()
    except Exception:
        return None
    if not thost or "dchub.cloud" in thost:
        return None
    try:
        here = (urlparse("https://" + (request.host or "")).hostname or "").lower()
    except Exception:
        here = ""
    if here and thost == here:
        return None
    return target


def _fetch_mirror(target: str) -> tuple[dict | None, str]:
    """One request, 4s ceiling, never raises. Returns (payload, error)."""
    import urllib.request
    url = target.rstrip("/") + "/api/v1/ops/origin-freshness"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "dchub-integrity-shell/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except Exception as e:
        return None, str(e)[:160]


def _lane_failover(c) -> list[dict]:
    out = []

    # 1a — THIS origin's own ingest heartbeat (pure DB). Establishes whether
    # the numbers on this very page can be trusted.
    if c is None:
        out.append(_check("fo_self_fresh", "this origin's ingest fresh (<6h)",
                          None, "no db"))
    else:
        age = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) "
                         "FROM mcp_tool_calls")
        try:
            from routes.failover_stale_gate import _is_failover
            role = "failover" if _is_failover() else "primary"
        except Exception:
            role = "unknown"
        out.append(_check(
            "fo_self_fresh", "this origin's ingest fresh (<6h)",
            (float(age) < 21600) if age is not None else None,
            (f"role={role} · newest row {round(float(age)/3600,1)}h ago"
             if age is not None else f"role={role} · no rows")))

    # 1b — THE LOAD-BEARING CHECK. The mirror's staleness is invisible from
    # our database because it is a DIFFERENT database. One cross-origin
    # probe is the only way to see it, and not seeing it is what cost a
    # morning on 2026-07-24.
    target = _probe_target()
    if not target:
        out.append(_check("fo_mirror_fresh", "failover mirror data is fresh",
                          None, "no safe probe target (self or edge) — skipped",
                          critical=True))
        out.append(_check("fo_mirror_gated", "stale mirror fails CLOSED (503)",
                          None, "mirror unreachable — cannot confirm"))
        return out

    host = urlparse(target).hostname or target
    payload, err = _fetch_mirror(target)
    if payload is None:
        # A mirror that does not answer is not a FAILURE — a dead mirror
        # cannot serve confident zeros. But it is not a pass either: we have
        # learned nothing about the thing this lane exists to watch, so the
        # check is critical and the lane renders "?" rather than green.
        # A 404 here specifically means the mirror has not yet deployed the
        # stale-gate, i.e. it is still capable of the 07-24 behaviour.
        hint = (" — mirror has not deployed the stale-gate yet, so it can "
                "still answer 200 with stale data") if "404" in err else ""
        out.append(_check("fo_mirror_fresh", "failover mirror data is fresh",
                          None, f"{host} unreachable: {err}{hint}",
                          critical=True))
        out.append(_check("fo_mirror_gated", "stale mirror fails CLOSED (503)",
                          None, "mirror unreachable — cannot confirm"))
        return out

    mage = payload.get("data_age_hours")
    mstale = bool(payload.get("stale"))
    out.append(_check(
        "fo_mirror_fresh", "failover mirror data is fresh",
        (not mstale) if mage is not None else None,
        (f"{host} data_age={mage}h (threshold {payload.get('threshold_hours')}h)"
         if mage is not None else f"{host} reports unknown age"),
        critical=True))

    # 1b(ii) — CODE drift, measured without a second network call by
    # comparing the mirror's commit to our own. Railway auto-deploys from
    # main, so the primary's SHA is effectively main's tip; a mirror on a
    # different SHA is behind.
    #
    # ★ This exists because the detector that was supposed to catch it
    # (brain_consistency_radar.check_render_pipeline_blocked) cannot work:
    # it compares /api/v1/version's `build`, which is a hand-maintained
    # constant reading 91 on BOTH origins, and it only fires when main's
    # newest commit is 2-24h old — on a repo where the brain pushes every
    # ~45min that window is essentially never open.
    try:
        from routes.failover_stale_gate import _commit as _self_commit
        mine = _self_commit()
    except Exception:
        mine = None
    theirs = payload.get("commit")
    if not mine or not theirs:
        out.append(_check(
            "fo_mirror_code", "mirror runs the same code as this origin",
            None,
            f"commit unavailable (self={mine or '?'} mirror={theirs or '?'}) — "
            "platform git env var not injected"))
    else:
        out.append(_check(
            "fo_mirror_code", "mirror runs the same code as this origin",
            mine == theirs,
            f"self={mine} mirror={theirs}"
            + ("" if mine == theirs else
               " — mirror is behind; Render auto-deploy is OFF by design "
               "(pipeline minutes), so it only moves when the deploy hook fires")))

    # 1c — if the mirror IS stale, the gate must be turning that into a 503
    # rather than a 200 full of zeros. A stale-but-gated mirror is SAFE; a
    # stale-and-serving mirror is the original bug.
    if not mstale:
        out.append(_check("fo_mirror_gated", "stale mirror fails CLOSED (503)",
                          None, "mirror is fresh — gate not exercised"))
    else:
        armed = bool(payload.get("gate_active"))
        out.append(_check(
            "fo_mirror_gated", "stale mirror fails CLOSED (503)", armed,
            ("gate ACTIVE — mirror 503s its metrics surfaces"
             if armed else
             "mirror is STALE and still answering 200 (gate_disabled="
             f"{payload.get('gate_disabled')}) — this is the 07-24 bug")))
    return out


# ── lane 2 · slug freeze (loop closure) ───────────────────────────────

def _lane_slug_freeze(c) -> list[dict]:
    if c is None:
        return [_check("sf_nodb", "slug lane needs db", None, "no db")]
    out = []

    # 2a/2b — pending must be 0 on BOTH tables. This deliberately audits the
    # legacy facilities table alongside discovered_facilities, because the
    # /facility 301 resolves against both. lint: legacy-facilities-ok
    fpend = _scalar(c, "SELECT COUNT(*) FROM facilities "
                       "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
    dpend = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                       "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
    if fpend is None or dpend is None:
        out.append(_check("sf_pending", "no unfrozen canonical_slug rows",
                          None, f"facilities={fpend} discovered={dpend} (query failed)"))
    else:
        total = int(fpend) + int(dpend)
        out.append(_check("sf_pending", "no unfrozen canonical_slug rows",
                          total == 0,
                          f"pending: facilities={fpend} discovered={dpend}"))

    # 2c — the LOOP, not the state. Freezing is set-once, so a green 2a today
    # says nothing about tomorrow unless something re-runs the backfill. We
    # measure the inflow that the loop has to keep up with: rows ingested in
    # the last 24h that arrived without a slug. Sustained non-zero here means
    # the scheduled freeze is missing or broken, which is precisely how 61
    # rows accumulated silently before 2026-07-24.
    # The live schema does NOT match the repo DDL here (routes/discovery_routes.py
    # declares created_at, production disagrees — the same live-vs-repo drift
    # already burned us on power_plants). Resolve the timestamp column from
    # information_schema instead of assuming one, and say so plainly if none
    # of the candidates exist rather than reporting a bare "query failed".
    tscol = _scalar(c, "SELECT column_name FROM information_schema.columns "
                       "WHERE table_name = 'discovered_facilities' "
                       "AND column_name IN "
                       "('created_at','discovered_at','first_seen','updated_at') "
                       "ORDER BY array_position("
                       "ARRAY['created_at','discovered_at','first_seen','updated_at'],"
                       " column_name) LIMIT 1")
    recent = None
    if tscol:
        # Some of these columns are TEXT in production, so cast defensively;
        # a bad row must not take the whole check down.
        recent = _scalar(
            c, "SELECT COUNT(*) FROM discovered_facilities "
               "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> '' "
               f"AND NULLIF({tscol}::text,'') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
               f"AND NULLIF({tscol}::text,'')::timestamptz >= now() - interval '24 hours'")
    if recent is None:
        out.append(_check("sf_inflow", "24h slug inflow is being drained", None,
                          f"no usable timestamp column on discovered_facilities "
                          f"(tried created_at/discovered_at/first_seen; found "
                          f"{tscol or 'none'}) — 2a is the binding check"))
    else:
        out.append(_check("sf_inflow", "24h slug inflow is being drained",
                          int(recent) == 0,
                          f"{recent} facilities ingested in 24h still unfrozen "
                          f"(by {tscol}) · drained by slug-freeze-daily.yml"))

    # 2d — old MD5(id) URLs must still 301 somewhere, or freezing the slug
    # just moves the 404 bleed rather than stopping it.
    aliases = _scalar(c, "SELECT COUNT(*) FROM facility_slug_aliases")
    out.append(_check("sf_aliases", "legacy id-scheme URLs have 301 aliases",
                      (aliases > 1000) if aliases is not None else None,
                      f"{aliases} aliases" if aliases is not None
                      else "alias table unavailable"))
    return out


# ── lane 3 · DCPI verdict spread ──────────────────────────────────────

def _lane_dcpi_spread(c) -> list[dict]:
    if c is None:
        return [_check("dc_nodb", "dcpi lane needs db", None, "no db")]
    out = []

    r = _row(c, "SELECT COUNT(*) FILTER (WHERE verdict='BUILD'),"
                " COUNT(*) FILTER (WHERE verdict='CAUTION'),"
                " COUNT(*) FILTER (WHERE verdict='AVOID'), COUNT(*)"
                " FROM market_power_scores"
                " WHERE computed_at > now() - interval '7 days'")
    if not r or not r[3]:
        out.append(_check("dc_spread", "verdict spread not degenerate (<=85% one bucket)",
                          None, "no market_power_scores rows in 7d"))
        out.append(_check("dc_builds", "enough BUILD markets to write about (>=10)",
                          None, "no rows"))
    else:
        builds, cautions, avoids, total = (int(x or 0) for x in r)
        dominant = max(builds, cautions, avoids)
        share = dominant / float(total)
        summary = (f"BUILD={builds} CAUTION={cautions} AVOID={avoids} "
                   f"(n={total}, dominant={round(share*100,1)}%)")
        out.append(_check("dc_spread", "verdict spread not degenerate (<=85% one bucket)",
                          share <= 0.85, summary))
        # The media arm can only write about BUILD markets. Five is why the
        # afternoon_pulse slot kept returning to Cheyenne.
        out.append(_check("dc_builds", "enough BUILD markets to write about (>=10)",
                          builds >= 10,
                          f"{builds} BUILD markets — the media arm's entire "
                          "subject pool for dcpi_leader/afternoon_pulse"))

    # 3c — WHY the spread collapsed. If unrelated markets carry byte-identical
    # (constraint, excess) pairs, they are inheriting one ISO-level fallback
    # score instead of market-level data, and identical inputs must produce
    # identical verdicts. This distinguishes "the thresholds are wrong" from
    # "the inputs are duplicated" — and it is the latter, so relabelling
    # would be the wrong fix.
    d = _row(c, "WITH latest AS ("
                "  SELECT DISTINCT ON (market_slug) market_slug,"
                "    round(constraint_score::numeric,1) AS cs,"
                "    round(excess_power_score::numeric,1) AS es"
                "  FROM market_power_scores"
                "  WHERE computed_at > now() - interval '30 days'"
                "    AND constraint_score IS NOT NULL"
                "    AND excess_power_score IS NOT NULL"
                "  ORDER BY market_slug, computed_at DESC), "
                "grp AS (SELECT cs, es, count(*) AS n FROM latest GROUP BY cs, es) "
                "SELECT COALESCE(SUM(n) FILTER (WHERE n > 1),0),"
                " COALESCE(SUM(n),0), COALESCE(MAX(n),0) FROM grp")
    if not d or not d[1]:
        out.append(_check("dc_dupe_scores", "market scores are market-level (not ISO fallback)",
                          None, "no scored markets to compare"))
    else:
        dup, tot, biggest = int(d[0] or 0), int(d[1] or 0), int(d[2] or 0)
        pct = round(dup * 100.0 / tot, 1) if tot else 0.0
        out.append(_check(
            "dc_dupe_scores", "market scores are market-level (not ISO fallback)",
            pct <= 25.0,
            f"{dup}/{tot} markets ({pct}%) share an identical "
            f"(constraint,excess) pair with another market · largest "
            f"identical cluster={biggest}"))
    return out


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator is NAMED but never fired.

_LANES = [
    ("failover_origin", "1 · Failover-origin integrity", _lane_failover,
     "repoint the mirror's DATABASE_URL at live Neon; until then the "
     "stale-gate (routes/failover_stale_gate.py) makes it fail CLOSED"),
    ("slug_freeze", "2 · Slug freeze (loop closed)", _lane_slug_freeze,
     "POST /api/v1/admin/slug/freeze — scheduled every 6h by "
     ".github/workflows/slug-freeze-daily.yml (plus the 23:xx heartbeat "
     "tick), then purge the sitemap"),
    ("dcpi_spread", "3 · DCPI verdict spread", _lane_dcpi_spread,
     "repair market-level DCPI inputs for ISO-fallback markets — do NOT "
     "relabel verdicts to fix the histogram (DCPI_RELAX_VERDICTS_ARM stays off)"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS integrity_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[integrity] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = _lane_verdict(checks)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "actuator": actuator, "checks": checks, "ms": ms,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})

    try:
        from routes.failover_stale_gate import origin_state
        origin = origin_state()
    except Exception as e:
        logger.debug("[integrity] origin_state unavailable: %s", e)
        origin = {"role": "unknown"}

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC; names an actuator per lane but fires "
                "nothing. Lanes 2-3 are pure-DB; lane 1 makes ONE 4s probe to "
                "the other origin (never to dchub.cloud, never to self). See "
                "routes/integrity_master_shell.py",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO integrity_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[integrity] snapshot insert failed: %s", e)
        try:
            c.close()
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


# ── routes ────────────────────────────────────────────────────────────

@integrity_master_shell_bp.route("/api/v1/admin/integrity/master-tick",
                                 methods=["GET", "POST"])
def integrity_master_tick():
    if _disabled():
        # 404 not 404: the CF worker's proxyWithRetry treats ANY 5xx from
        # Railway as a dead-origin signal and fails over to the stale Render
        # backend — 2 within 10s trip the breaker site-wide for 30s. A kill
        # switch must NEVER return 5xx.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@integrity_master_shell_bp.route("/admin/integrity", methods=["GET"])
@integrity_master_shell_bp.route("/api/v1/admin/integrity", methods=["GET"])
def integrity_dashboard():
    if _disabled():
        return Response("integrity shell disabled", status=404)
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
            f"<tr><td style='padding:4px 8px'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    # Origin-authoritative banner. The whole point of this shell is that an
    # ops page must say WHO answered it.
    try:
        from routes.failover_stale_gate import banner_html
        banner = banner_html()
    except Exception:
        banner = ""

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Integrity Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Integrity Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#25 · 07-24 · served by "
        f"<b>{_esc(str(p.get('origin',{}).get('role','?')))}</b> origin · read-only "
        f"DIAGNOSTIC (names an actuator per lane, fires nothing) · 30s tick cache · "
        f"auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/integrity/master-tick</div>"
        + banner + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
