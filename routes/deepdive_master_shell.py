"""
routes/deepdive_master_shell.py — Deep-Dive Command Deck (2026-07-08 wave).

ONE cockpit for the 07-08 flywheel deep dive. It does NOT re-probe what the
existing master shells already measure — it AGGREGATES their verdicts (flywheel /
backfunnel / registry-freshness / media) into a single pane and ADDS the two
lanes this deep dive created plus a product-build tracker:

  1 · FIX-SIGNAL TRUST  (new, direct probe) — is the brain's fix_success signal
      trustworthy enough to arm its hands? Reads /brain/self-model (verified real
      effects + sample size) + causal availability. The gate that governs
      whether auto-merge can safely leave dry-run.
  2 · AUTONOMY ARM      (new, env posture) — the actual arm state + a SAFETY
      check: never "live hands on a bad signal". Surfaces the owner decision to
      drop SENTINEL_AUTO_MERGE_DRY_RUN for the safest (site_integrity) domain.
  3 · FLYWHEEL          (aggregate) — routes/flywheel_master_shell verdict.
  4 · BACK-OF-FUNNEL    (aggregate) — routes/backfunnel_master_shell verdict
      (retention / attribution / demand).
  5 · REGISTRY FRESH    (aggregate) — routes/registry_freshness_master_shell.
  6 · MEDIA             (aggregate) — routes/media_master_shell (score + starved).
  7 · PRODUCT BUILD     (new) — ship-state of the four 07-08 enhancements:
      one-pager emit (live probe), WRI water ingest (db probe), grid-headroom
      tier + retention pitch (MCP-server env flags — surfaced informationally
      since this backend shell can't read the MCP process env).

The pane also computes an OWNER-ACTIONS strip — the decisions on the operator's
desk, derived from live lane state (arm auto-merge / pay X / refresh Glama) — but
it NEVER flips any of them. Read-only by construction, same discipline as every
prior wave.

Design mirrors routes/fixwave_master_shell.py: admin-gated, killable
(DEEPDIVE_DISABLED=1), loopback self-calls for backend-own admin endpoints (a
public CF round-trip would 503 a fan-out tick), every probe fail-soft +
timeout-bounded, snapshot row per tick, 30s cache. READ-ONLY — never mutates.

Endpoints:
  GET/POST /api/v1/admin/deepdive/master-tick   JSON scoreboard (7 lanes)
  GET      /admin/deepdive                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/deepdive                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as fixwave / growth / backfunnel shells.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

deepdive_master_shell_bp = Blueprint("deepdive_master_shell", __name__)

# ── targets ───────────────────────────────────────────────────────────
# Loopback for backend-own admin endpoints (mirrors fixwave/growth shells —
# public round-trips through CF would 503 a fan-out tick past ~15s, and the
# sub-shells are themselves 30s-cached so repeat loopback hits are cheap).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE",
                        "https://dchub-backend-production.up.railway.app"))

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("DEEPDIVE_DISABLED") or "").strip() == "1"


def _admin_hdr() -> dict:
    key = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    return {"X-Admin-Key": key} if key else {}


# ── helpers ───────────────────────────────────────────────────────────

def _http(url: str, *, method: str = "GET", headers: dict | None = None,
          body: dict | None = None, timeout: float = 8.0) -> tuple[int, str, int]:
    """Bounded fetch. Returns (status, body_text[:200k], elapsed_ms).
    Never raises — network failure returns (0, err, ms)."""
    t0 = time.time()
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", _BROWSER_UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read(200_000).decode("utf-8", "replace"), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(50_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, int((time.time() - t0) * 1000)
    except Exception as e:  # DNS, timeout, TLS, conn-refused
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def _conn():
    """Raw psycopg2 connection (mirrors fixwave._conn). None on failure."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.debug("[deepdive] conn failed: %s", e)
        return None


def _check(cid: str, name: str, passed, detail: str, ms: int = 0) -> dict:
    # passed: True / False / None (None = indeterminate, shown as "?")
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:300], "ms": ms}


def _envflag(name: str) -> bool:
    return (os.environ.get(name) or "").strip() in ("1", "true", "True", "yes", "on")


def _jget(url: str, timeout: float = 8.0):
    """Loopback GET returning (json_or_None, status, ms)."""
    st, body, ms = _http(url, headers=_admin_hdr(), timeout=timeout)
    if st != 200:
        return None, st, ms
    try:
        return json.loads(body), st, ms
    except Exception:
        return None, st, ms


# ── lane 1 · fix-signal trust ────────────────────────────────────────

def _lane_fix_signal(c) -> list[dict]:
    out = []
    j, st, ms = _jget(f"{_BACKEND_BASE}/api/v1/brain/self-model?_cb={int(time.time())}")
    if j is None:
        return [_check("selfmodel", "brain self-model reachable", None, f"HTTP {st}", ms)]
    cur = (j.get("current_state") or {}) if isinstance(j, dict) else {}
    # R3: the HONEST rate is the verified-real-effect one; self-model exposes it
    rate = (j.get("fix_success_rate") if j.get("fix_success_rate") is not None
            else cur.get("fix_success_rate_30d"))
    sample = (j.get("verified_sample") if j.get("verified_sample") is not None
              else cur.get("verified_sample"))
    try:
        rate_f = float(rate) if rate is not None else None
    except Exception:
        rate_f = None
    try:
        sample_i = int(sample) if sample is not None else None
    except Exception:
        sample_i = None
    # Trustworthy = a non-null verified rate over a real sample (guard: >=5, per R3).
    trustworthy = (rate_f is not None and sample_i is not None and sample_i >= 5)
    out.append(_check(
        "signal_trust", "fix_success is verified-effect over a real sample (>=5)",
        trustworthy,
        (f"rate={rate_f:.3f} · sample={sample_i}" if rate_f is not None
         else "no verified rate yet — signal not trustworthy"), ms))
    if rate_f is not None:
        out.append(_check(
            "gate_50", "verified fix_success >= 0.50 (auto-merge arm gate)",
            rate_f >= 0.50, f"rate={rate_f:.3f} (gate 0.50)", 0))
    reasoning = bool((j.get("learning") or {}).get("reasoning_online")
                     or j.get("reasoning_online"))
    out.append(_check("reasoning_online", "reasoning layer online (ANTHROPIC_API_KEY)",
                      reasoning if (j.get("reasoning_online") is not None
                                    or (j.get("learning") or {}).get("reasoning_online") is not None)
                      else None, "self-model", 0))
    # causal availability — the janitor's resolved-close arm depends on it
    cj, cst, cms = _jget(f"{_BACKEND_BASE}/api/v1/brain/causal?_cb={int(time.time())}")
    chains = 0
    if isinstance(cj, dict):
        an = cj.get("analysis") or {}
        if isinstance(an, dict):
            chains = len(an.get("causal_chains") or an.get("chains") or [])
        if not chains and isinstance(cj.get("chains"), list):
            chains = len(cj["chains"])
    out.append(_check("causal_live", "L14 causal chains available (janitor resolved-arm)",
                      (chains > 0) if cj is not None else None,
                      f"{chains} chains" if cj is not None else f"HTTP {cst}", cms))
    return out


# ── lane 2 · autonomy arm posture ────────────────────────────────────

def _lane_autonomy(c) -> list[dict]:
    out = []
    automerge_on = _envflag("L22_AUTO_MERGE_ENABLE")
    dry_run = _envflag("SENTINEL_AUTO_MERGE_DRY_RUN")
    janitor_verify = _envflag("BRAIN_JANITOR_VERIFY_COMPLETED")
    autonomy = _envflag("BRAIN_AUTONOMY_ENABLED")
    # env this SERVICE sees (web); the brain lives on the worker so treat as
    # indicative, not authoritative — say so in the detail.
    out.append(_check("autonomy_on", "BRAIN_AUTONOMY_ENABLED (opens draft PRs)",
                      autonomy, "on" if autonomy else "off · (env as seen by web svc)", 0))
    out.append(_check("automerge_state", "L22_AUTO_MERGE_ENABLE / SENTINEL_AUTO_MERGE_DRY_RUN",
                      None,
                      f"automerge={'on' if automerge_on else 'off'} · "
                      f"dry_run={'on' if dry_run else 'OFF(live)'} · (web-svc env)", 0))
    # SAFETY posture: never live hands while the signal is unproven. This check
    # FAILS loudly if automerge is live (dry_run off) — that's only ever safe
    # after the fix-signal lane is green, which the operator confirms manually.
    safe_posture = (not automerge_on) or dry_run
    out.append(_check("safe_posture", "not 'live hands' yet (automerge off OR dry-run on)",
                      safe_posture,
                      "staged/safe" if safe_posture else "LIVE auto-merge — confirm signal first", 0))
    out.append(_check("janitor_honest_completed",
                      "janitor verified-completed path armed (BRAIN_JANITOR_VERIFY_COMPLETED)",
                      None, "on" if janitor_verify else "off (default) — arm after review", 0))
    return out


# ── lanes 3-6 · aggregate the existing shells ────────────────────────

def _agg_lane(path: str, cid: str) -> tuple[list[dict], dict | None]:
    j, st, ms = _jget(f"{_BACKEND_BASE}{path}")
    if j is None:
        return [_check(f"{cid}_tick", f"{cid} master-tick reachable", None, f"HTTP {st}", ms)], None
    lp, lt = j.get("lanes_pass"), j.get("lanes_total")
    rows = []
    if lp is not None and lt is not None:
        rows.append(_check(f"{cid}_green", f"{cid}: all lanes green",
                           lp == lt and lt > 0, f"{lp}/{lt} lanes green", ms))
        for l in (j.get("lanes") or []):
            rows.append(_check(f"{cid}_{l.get('lane')}", str(l.get("label") or l.get("lane")),
                               bool(l.get("pass")), str(l.get("progress") or ""), 0))
    return rows, j


def _lane_flywheel(c) -> list[dict]:
    return _agg_lane("/api/v1/admin/flywheel/master-tick", "flywheel")[0]


def _lane_backfunnel(c) -> list[dict]:
    return _agg_lane("/api/v1/admin/backfunnel/master-tick", "backfunnel")[0]


def _lane_registry(c) -> list[dict]:
    return _agg_lane("/api/v1/admin/registry-freshness/master-tick", "registry")[0]


def _lane_media(c) -> list[dict]:
    j, st, ms = _jget(f"{_BACKEND_BASE}/api/v1/admin/media/master-tick")
    if j is None:
        return [_check("media_tick", "media master-tick reachable", None, f"HTTP {st}", ms)]
    score = j.get("media_score")
    starved = ((j.get("tier2_score") or {}).get("starved") if isinstance(j.get("tier2_score"), dict) else None)
    out = [_check("media_score", "media score >= 50",
                  (int(score) >= 50) if score is not None else None,
                  j.get("headline") or (f"score {score}" if score is not None else "n/a"), ms)]
    out.append(_check("media_not_starved", "media not reach-starved",
                      (starved is False) if starved is not None else None,
                      "not starved" if starved is False else ("STARVED — reach constraint" if starved else "n/a"), 0))
    return out


# ── lane 7 · product build ───────────────────────────────────────────

def _water_wri_rows(c) -> int | None:
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM water_risk "
                        "WHERE LOWER(COALESCE(source,'')) LIKE 'wri%%'")
            return int((cur.fetchone() or [0])[0])
    except Exception as e:
        logger.debug("[deepdive] water_risk probe skipped: %s", e)
        return None


def _lane_product(c) -> list[dict]:
    out = []
    # 1) one-pager emit — generate_site_analysis should return a signed pdf_report_url
    #    (Ashburn coords; premium json). Real live probe.
    j, st, ms = _jget(
        f"{_BACKEND_BASE}/api/v1/site-report?lat=39.0438&lon=-77.4874"
        f"&form=premium&format=json&prepared_for=DeepDive%20Probe", timeout=12.0)
    has_url = bool(isinstance(j, dict) and j.get("pdf_report_url"))
    out.append(_check("onepager", "one-pager: generate_site_analysis emits branded pdf_report_url",
                      has_url if j is not None else None,
                      "pdf_report_url present" if has_url else f"HTTP {st}", ms))
    # 2) WRI water ingest — rank_sites water objective flips ON only when real rows land
    wr = _water_wri_rows(c)
    out.append(_check("water_ingest", "WRI water rows present (rank_sites water objective enables)",
                      (wr is not None and wr > 0) if wr is not None else None,
                      (f"{wr} WRI rows" if wr is not None else "db unavailable"), 0))
    # 3+4) MCP-server env flags — this backend shell cannot read the MCP process
    #      env, so these are surfaced informationally (arm on the MCP service).
    out.append(_check("grid_headroom_flag", "grid-headroom tier (MCP: DCHUB_GRID_HEADROOM_TIER)",
                      None, "arm on dchub-mcp-server env", 0))
    out.append(_check("retention_pitch_flag", "retention pitch (MCP: DCHUB_RETENTION_PITCH_ENABLED)",
                      None, "arm on dchub-mcp-server env", 0))
    return out


# ── lane registry ────────────────────────────────────────────────────

_LANES = [
    ("fix_signal", "1 · Fix-signal trust — is the brain's success signal real?", _lane_fix_signal),
    ("autonomy", "2 · Autonomy arm — safe posture + the arm decision", _lane_autonomy),
    ("flywheel", "3 · Flywheel (aggregate)", _lane_flywheel),
    ("backfunnel", "4 · Back-of-funnel: retention / attribution / demand (aggregate)", _lane_backfunnel),
    ("registry", "5 · Registry freshness (aggregate)", _lane_registry),
    ("media", "6 · Media score + reach (aggregate)", _lane_media),
    ("product", "7 · Product build — the four 07-08 enhancements", _lane_product),
]


# ── owner-actions strip (computed from lane state; NEVER acts) ────────

def _owner_actions(lanes: list[dict]) -> list[str]:
    by = {l["lane"]: l for l in lanes}
    acts = []
    fix = by.get("fix_signal") or {}
    fix_checks = {ch["id"]: ch for ch in fix.get("checks", [])}
    auton = by.get("autonomy") or {}
    aut_checks = {ch["id"]: ch for ch in auton.get("checks", [])}
    signal_ok = (fix_checks.get("signal_trust", {}).get("pass") is True
                 and fix_checks.get("gate_50", {}).get("pass") is True)
    dry = aut_checks.get("safe_posture", {}).get("pass") is True
    if signal_ok and dry:
        acts.append("🔓 Fix-signal is trustworthy AND ≥0.50 — READY to drop "
                    "SENTINEL_AUTO_MERGE_DRY_RUN→0 for site_integrity (safest domain, reversible).")
    elif not signal_ok:
        acts.append("⏳ Hold auto-merge: fix-signal not yet verified-trustworthy — "
                    "don't arm hands on a noisy signal.")
    media = by.get("media") or {}
    if any(ch["id"] == "media_not_starved" and ch["pass"] is False for ch in media.get("checks", [])):
        acts.append("💳 Media reach-starved — decide X paid tier (~$200/mo) or redirect to "
                    "LinkedIn + connector directories.")
    reg = by.get("registry") or {}
    if reg.get("pass") is False:
        acts.append("🖱️ Registry not fully fresh — click Glama Refresh (owner) + keep public SoT current.")
    prod = by.get("product") or {}
    if any(ch["id"] == "water_ingest" and (ch["pass"] in (False, None)) for ch in prod.get("checks", [])):
        acts.append("💧 Water objective still gated — run the WRI ingest once, verify rows, "
                    "then rank_sites water auto-enables.")
    return acts


# ── tick ─────────────────────────────────────────────────────────────

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS deepdive_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[deepdive] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn in _LANES:
        try:
            checks = fn(c)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "owner_actions": _owner_actions(lanes),
        "note": ("read-only cockpit; aggregates flywheel/backfunnel/registry/media + "
                 "adds fix-signal-trust, autonomy-arm, product-build. "
                 "see routes/deepdive_master_shell.py"),
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO deepdive_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[deepdive] snapshot insert failed: %s", e)
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

@deepdive_master_shell_bp.route("/api/v1/admin/deepdive/master-tick", methods=["GET", "POST"])
def deepdive_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    fresh = (request.args.get("fresh") or "") == "1"
    if fresh:
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@deepdive_master_shell_bp.route("/admin/deepdive", methods=["GET"])
@deepdive_master_shell_bp.route("/api/v1/admin/deepdive", methods=["GET"])
def deepdive_dashboard():
    if _disabled():
        return Response("deepdive disabled", status=503)
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
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}"
            f"{(' · ' + str(ch['ms']) + 'ms') if ch.get('ms') else ''}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table></div>")

    actions = p.get("owner_actions") or []
    actions_html = ""
    if actions:
        items = "".join(f"<li style='margin:4px 0'>{_esc(a)}</li>" for a in actions)
        actions_html = (
            "<div style='background:#1e1b4b;border:1px solid #6366f1;border-radius:12px;"
            "padding:14px 18px;margin:12px 0'>"
            "<div style='font-weight:700;color:#c7d2fe'>Decisions on your desk (this shell never flips them)</div>"
            f"<ul style='margin:8px 0 0;padding-left:20px'>{items}</ul></div>")

    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Deep-Dive Command Deck · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Deep-Dive Command Deck "
        f"<span style='color:{'#22c55e' if p['lanes_pass'] == p['lanes_total'] else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-08 deep-dive wave · read-only cockpit · "
        f"30s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/deepdive/master-tick</div>"
        + actions_html + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
