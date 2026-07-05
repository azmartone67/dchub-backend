"""routes/coverage_master_shell.py — Coverage & Media Master Shell (2026-07-04).

Purpose: keep the PUBLIC "coverage" story both accurate AND safe to broadcast.
The /coverage ("What's New") page and /api/v1/whats-new feed publish live DB
counts. The counts are real, but two things can turn them into a liability the
moment DC Hub Media puts them in a blog / email / LinkedIn / X post:

  1. FRAMING — "21,882 data centers" is the RAW tracked count; only ~4.8K are
     verified (deduped, is_duplicate=0). Most of the big layers (substations,
     transmission, gas, fiber, middle-mile, EIA plants) are UNIFIED third-party
     open data (HIFLD / FCC / EIA), not DC Hub discoveries. Say "tracked" +
     "verified", and "unify" not "discover", or a competitor/journalist will.
  2. FUTURE DATE — the page showed "updated 2026-07-05" (generated_at rendered
     in UTC tips into tomorrow for US readers). Render data_as_of (a real
     snapshot DATE), never generated_at.

This shell (read-only probes, house master-shell pattern):
  • scores the honesty levers PASS/FAIL against the LIVE feed, and
  • GENERATES ready-to-send external messaging drafts (LinkedIn / X / email /
    blog) built from the verified numbers, leading with MOTION (weekly adds,
    deal velocity, unified + open) — never the raw headline inventory.

Drafts are DRAFTS: this never auto-posts (media POSITIVE policy — publishing is
env-gated elsewhere). It hands Media copy that's already been honesty-checked.

Routes:
  GET/POST /api/v1/admin/coverage/master-tick   JSON scoreboard + drafts
  GET      /admin/coverage                       HTML dashboard (chips + copy)
  GET      /api/v1/admin/coverage               (alias of the dashboard)

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY). Kill switch: COVERAGE_SHELL_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, date
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

coverage_master_shell_bp = Blueprint("coverage_master_shell", __name__)

# Loopback for backend-own endpoints (mirrors fixwave/growth master shells —
# a fan-out tick round-tripping through CF would 503 past ~15s).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE",
                        "https://dchub-backend-production.up.railway.app"))

_TICK_TTL = 60  # seconds; the feed is 30-min cached anyway
_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("COVERAGE_SHELL_DISABLED") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _http(url: str, *, timeout: float = 10.0) -> tuple[int, str, int]:
    """Bounded GET. Returns (status, body[:200k], ms). Never raises."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-coverage-shell/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read(200_000).decode("utf-8", "replace"), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(50_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, int((time.time() - t0) * 1000)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def _get_json(path: str) -> dict:
    st, body, _ms = _http(f"{_BACKEND_BASE}{path}")
    if st != 200:
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {}


def _check(cid: str, name: str, passed, detail: str) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": (detail or "")[:300]}


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt(n) -> str:
    n = _num(n)
    return f"{n:,}" if n is not None else "—"


# ── fact extraction from the LIVE public feed ─────────────────────────

def _facts() -> dict:
    """Pull the numbers Media would quote, straight from the live surfaces the
    public sees — so the drafts can never drift from what's on the site."""
    feed = _get_json("/api/v1/whats-new")
    stats = (_get_json("/api/v1/stats") or {}).get("data", {})
    items = {i.get("category"): i for i in (feed.get("items") or [])}

    dc = items.get("Data centers", {})
    deals = items.get("Data-center deals", {})
    subs = items.get("Substations", {})
    fiber = items.get("Fiber routes", {})
    mid = items.get("Broadband / middle-mile coverage", {})

    return {
        "feed_ok": bool(feed.get("ok")),
        "feed": feed,
        "items": items,
        "total_added_7d": _num(feed.get("total_added")),
        "data_as_of": feed.get("data_as_of"),
        "generated_at": feed.get("generated_at"),
        # facilities — tracked vs verified (the crux)
        "dc_tracked": _num(dc.get("total")) or _num(feed.get("facilities_tracked")),
        "dc_verified": _num(dc.get("verified")) or _num(feed.get("facilities_verified")),
        "dc_added_7d": _num(dc.get("added")),
        # curated velocity — the strongest number
        "deals_total": _num(deals.get("total")),
        "deals_7d": _num(deals.get("added")),
        "deals_1d": _num(deals.get("added_1d")),
        # unified public layers
        "substations": _num(subs.get("total")),
        "fiber_routes": _num(fiber.get("total")),
        "middle_mile": _num(mid.get("total")),
        # geography
        "countries": _num(stats.get("countries")) or _num(stats.get("total_countries")),
    }


# ── messaging drafts (honest, motion-first) ───────────────────────────

_BANNED = [
    # never assert the raw tracked count as "verified"
    (re.compile(r"2[01],?\d{3}\s+verified", re.I), "raw count asserted as 'verified'"),
    (re.compile(r"verified\s+2[01],?\d{3}", re.I), "raw count asserted as 'verified'"),
    # never claim we "discovered/mapped" a third-party public layer
    (re.compile(r"(discover|mapp?ed|built)\D{0,30}(middle[- ]mile|substation|HIFLD|FCC)", re.I),
     "public layer claimed as a DC Hub discovery"),
]
_REQUIRED = ["tracked", "verified", "CC-BY"]  # every draft-set must carry the honest framing


def _drafts(f: dict) -> dict:
    dc_v = _fmt(f["dc_verified"]);  dc_t = _fmt(f["dc_tracked"])
    deals_7 = _fmt(f["deals_7d"]);  deals_t = _fmt(f["deals_total"])
    added = _fmt(f["total_added_7d"])
    subs = _fmt(f["substations"]);  mid = _fmt(f["middle_mile"])
    ctry = _fmt(f["countries"])

    linkedin = (
        f"Every week the map of the AI build-out gets denser.\n\n"
        f"In the last 7 days DC Hub added {added} infrastructure records — including "
        f"{deals_7} new data-center deals (now {deals_t} tracked). Refreshed daily, open "
        f"under CC-BY-4.0.\n\n"
        f"What makes it different isn't one big number — it's the motion and the "
        f"unification. {dc_v}+ verified data centers inside a {dc_t}-facility tracked "
        f"frontier, stitched together with the grid ({subs}+ substations), gas, and "
        f"fiber / middle-mile ({mid}+ records) in ONE queryable layer — machine-readable "
        f"over MCP, no signup.\n\n"
        f"Analysts ship quarterly PDFs. We ship a living dataset your agents can query "
        f"today, across {ctry} countries.\n\n"
        f"→ dchub.cloud   ·   Data: DC Hub (dchub.cloud), CC-BY-4.0"
    )

    x = (
        f"The AI build-out map got denser this week:\n"
        f"+{added} infra records on DC Hub, incl. +{deals_7} data-center deals "
        f"({deals_t} tracked).\n\n"
        f"{dc_v}+ verified DCs in a {dc_t}-tracked frontier + grid/gas/fiber, "
        f"unified. Daily-refreshed, CC-BY-4.0, queryable over MCP.\n\ndchub.cloud"
    )

    email_subject = f"{deals_7} new data-center deals landed this week — here's the live feed"
    email = (
        f"Subject: {email_subject}\n\n"
        f"Hi {{first_name}},\n\n"
        f"A quick pulse on the physical AI build-out, straight from DC Hub's live database.\n\n"
        f"Last 7 days:\n"
        f"  • {deals_7} new data-center deals logged (now {deals_t} tracked)\n"
        f"  • {added} new infrastructure records across all layers\n"
        f"  • {dc_v}+ verified data centers — inside a {dc_t}-facility tracked frontier — "
        f"across {ctry} countries\n\n"
        f"Unlike a quarterly PDF, this refreshes daily and it's queryable: facilities, "
        f"deals, grid ({subs}+ substations), gas, and fiber / middle-mile ({mid}+ records) "
        f"in one layer, machine-readable over MCP, open under CC-BY-4.0.\n\n"
        f"See the live coverage feed: https://dchub.cloud/whats-new\n"
        f"Query it (no signup): https://dchub.cloud/playground\n\n"
        f"— The DC Hub team\n"
        f"Data: DC Hub (dchub.cloud), CC-BY-4.0"
    )

    blog = (
        f"## The AI build-out, one week at a time\n\n"
        f"Infrastructure isn't a snapshot — it's a moving target. In the last 7 days, "
        f"DC Hub added **{added} records**: {deals_7} new data-center deals (now {deals_t} "
        f"tracked), plus fresh grid, gas, and fiber.\n\n"
        f"The value isn't the size of any one table — it's that these layers live in one "
        f"place and move every day:\n\n"
        f"- **Data centers** — {dc_v}+ verified, inside a {dc_t}-facility tracked frontier, "
        f"across {ctry} countries (DC Hub crawls & curates these)\n"
        f"- **Grid & energy** — {subs}+ substations plus transmission and power plants "
        f"(we unify HIFLD / EIA open data)\n"
        f"- **Fiber & connectivity** — routes plus {mid}+ middle-mile records (unified FCC + "
        f"DC Hub)\n"
        f"- **Deals** — {deals_t} tracked transactions, curated by DC Hub\n\n"
        f"All refreshed daily, open under CC-BY-4.0, and queryable by agents over MCP — no "
        f"signup. Cite it as *DC Hub (dchub.cloud)*.\n"
    )

    return {
        "linkedin": linkedin,
        "x": x,
        "email_subject": email_subject,
        "email": email,
        "blog": blog,
    }


def _messaging_violations(drafts: dict) -> list[str]:
    """Scan the generated copy for banned framings + required honesty tokens.
    Guards against a future edit reintroducing the exact liability this shell
    exists to prevent."""
    blob = "\n".join(str(v) for v in drafts.values())
    viol = []
    for rx, why in _BANNED:
        m = rx.search(blob)
        if m:
            viol.append(f"{why}: “{m.group(0)[:60]}”")
    for tok in _REQUIRED:
        if tok.lower() not in blob.lower():
            viol.append(f"missing required honesty token: '{tok}'")
    return viol


# ── levers ────────────────────────────────────────────────────────────

def _lane_honesty(f: dict) -> list[dict]:
    out = []
    dc_t, dc_v = f["dc_tracked"], f["dc_verified"]
    out.append(_check(
        "feed_live", "public /api/v1/whats-new answers 200 + ok",
        f["feed_ok"], "feed ok" if f["feed_ok"] else "feed unavailable"))
    out.append(_check(
        "verified_present", "feed exposes a verified data-center sub-count",
        dc_v is not None, f"verified={_fmt(dc_v)}"))
    out.append(_check(
        "tracked_gt_verified", "tracked > verified (so headline can't read '21.9K verified')",
        (dc_t is not None and dc_v is not None and dc_t > dc_v),
        f"tracked={_fmt(dc_t)} · verified={_fmt(dc_v)}"))
    return out


def _lane_date(f: dict) -> list[dict]:
    out = []
    da = f["data_as_of"]
    ok_present = bool(da)
    out.append(_check("data_as_of_present", "feed carries data_as_of (a snapshot DATE)",
                      ok_present, f"data_as_of={da}"))
    not_future = None
    if da:
        try:
            not_future = date.fromisoformat(str(da)[:10]) <= datetime.now(timezone.utc).date()
        except Exception:
            not_future = None
    out.append(_check("data_as_of_not_future", "data_as_of is not in the future",
                      not_future, f"data_as_of={da} vs today(UTC)={datetime.now(timezone.utc).date()}"))
    return out


def _lane_provenance(f: dict) -> list[dict]:
    out = []
    items = list((f.get("feed") or {}).get("items") or [])
    labeled = [i for i in items if i.get("provenance")]
    out.append(_check("all_items_provenanced", "every coverage item carries a provenance label",
                      bool(items) and len(labeled) == len(items),
                      f"{len(labeled)}/{len(items)} labeled"))
    # public layers must be labeled 'public' (not implied as DC Hub discoveries)
    pub_names = {"Substations", "Fiber routes", "Broadband / middle-mile coverage",
                 "Gas pipelines", "Power plants", "Transmission lines"}
    mislabeled = [i.get("category") for i in items
                  if i.get("category") in pub_names and i.get("provenance") != "public"]
    out.append(_check("public_layers_labeled_public", "third-party layers labeled provenance='public'",
                      not mislabeled, "ok" if not mislabeled else f"mislabeled: {mislabeled}"))
    return out


def _lane_messaging(f: dict) -> list[dict]:
    out = []
    drafts = _drafts(f)
    viol = _messaging_violations(drafts)
    out.append(_check("messaging_safe", "generated drafts pass the honesty/banned-framing scan",
                      not viol, "clean" if not viol else "; ".join(viol)))
    # sanity: the numbers actually interpolated (no bare em-dash placeholders)
    interpolated = all(f.get(k) is not None for k in ("deals_7d", "deals_total", "dc_verified", "dc_tracked"))
    out.append(_check("messaging_numbers_present", "drafts interpolated live numbers (no missing facts)",
                      interpolated, "all facts present" if interpolated else "some facts missing from feed"))
    return out


_LANES = [
    ("facilities_honesty", "1 · Facilities: tracked vs verified", _lane_honesty),
    ("no_future_date",     "2 · Date: as_of, never future",       _lane_date),
    ("provenance",         "3 · Provenance labeled (unify≠discover)", _lane_provenance),
    ("messaging",          "4 · Messaging drafts honesty-safe",    _lane_messaging),
]


def _run_tick() -> dict:
    f = _facts()
    lanes = []
    for key, label, fn in _LANES:
        try:
            checks = fn(f)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass, "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_as_of": f.get("data_as_of"),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "facts": {k: v for k, v in f.items() if k not in ("feed", "items")},
        "drafts": _drafts(f),
        "note": "read-only probes over the live public feed; drafts are honesty-checked, "
                "NOT auto-posted. See routes/coverage_master_shell.py",
    }


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

@coverage_master_shell_bp.route("/api/v1/admin/coverage/master-tick", methods=["GET", "POST"])
def coverage_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@coverage_master_shell_bp.route("/admin/coverage", methods=["GET"])
@coverage_master_shell_bp.route("/api/v1/admin/coverage", methods=["GET"])
def coverage_dashboard():
    if _disabled():
        return Response("coverage shell disabled", status=503)
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
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table></div>")

    drafts = p.get("drafts", {})
    def _draft_block(title, key):
        val = drafts.get(key, "")
        return (f"<div style='margin:14px 0'>"
                f"<div style='font-weight:700;font-size:14px;margin-bottom:6px'>{_esc(title)}</div>"
                f"<textarea readonly style='width:100%;height:{ '150px' if key in ('linkedin','email','blog') else '90px'};"
                f"background:#0b1220;color:#cbd5e1;border:1px solid #334155;border-radius:8px;"
                f"padding:10px;font:13px/1.5 ui-monospace,Menlo,monospace' "
                f"onclick='this.select()'>{_esc(val)}</textarea></div>")

    facts = p.get("facts", {})
    facts_line = (
        f"tracked DCs {_fmt(facts.get('dc_tracked'))} · verified {_fmt(facts.get('dc_verified'))} · "
        f"deals +{_fmt(facts.get('deals_7d'))}/7d ({_fmt(facts.get('deals_total'))} total) · "
        f"+{_fmt(facts.get('total_added_7d'))} records/7d · {_fmt(facts.get('countries'))} countries · "
        f"as_of {_esc(str(facts.get('data_as_of')))}")

    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Coverage & Media Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:900px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Coverage &amp; Media Master Shell "
        f"<span style='color:{'#22c55e' if p['lanes_pass'] == p['lanes_total'] else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} levers green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>honesty probes over the live /api/v1/whats-new feed · "
        f"read-only · drafts NOT auto-posted · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/coverage/master-tick</div>"
        f"<div style='color:#38bdf8;font-size:12px;margin-top:6px'>{facts_line}</div>"
        + "".join(cards)
        + "<h3 style='margin:22px 0 4px'>Ready-to-send drafts "
          "<span style='color:#64748b;font-weight:400;font-size:13px'>(honesty-checked · click to select)</span></h3>"
        + _draft_block("LinkedIn", "linkedin")
        + _draft_block("X / Twitter", "x")
        + _draft_block(f"Email — subject: {drafts.get('email_subject','')}", "email")
        + _draft_block("Blog", "blog")
        + "</body>")
    return Response(html, mimetype="text/html")


def register_coverage_master_shell(app):
    try:
        app.register_blueprint(coverage_master_shell_bp)
        logger.info("[coverage-shell] registered")
    except Exception as e:
        logger.warning("[coverage-shell] register failed: %s", e)
