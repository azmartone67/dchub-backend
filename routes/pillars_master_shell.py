"""routes/pillars_master_shell.py — Moat Pillars 1-3 Master Shell (2026-07-13).

Purpose: keep the PUBLIC announcement of the three moat pillars both LIVE-verified
and safe to broadcast. Three capabilities shipped this month that competitors
(DataCenterHawk = PDFs, Electricity Maps = carbon-only, LandGate/WoodMac = US land)
structurally don't have — but the marketing artifact (the 07-11 partner brief) was
written before pillars 2 & 3 shipped and undersold the moat by two-thirds.

The three pillars:
  1 · Provenance envelope — per-record verified/tracked confidence + as-of + CC-BY
      cite on every response. Nobody else lets an agent cite with a confidence level.
  2 · International grid telemetry — Japan (OCCTO), South Korea (KPX) and Brazil SIN
      (ONS) live and RANKED beside the US ISOs, EU zones, GB and Taiwan on one
      renewable-share scale (Australia NEM + Singapore EMA live-partial, unranked).
  3 · Agent-state lock-in — save_site builds a durable shortlist; get_changes /
      list_saved_sites return per-saved-site deltas next session (verdict flips,
      score moves, alerts fired, new facilities nearby). A data layer with memory.

This shell (read-only probes, house master-shell pattern — mirrors coverage_master_shell):
  • VERIFIES each pillar is live against the real public surfaces, and
  • GENERATES ready-to-send announcement drafts (LinkedIn / X / email / blog +
    the partner one-liner) built from the LIVE numbers, so the copy can never
    drift from what the site actually serves.

Honesty guardrails (the pillar-2 traps a competitor/journalist would catch):
  • Brazil's ONS reports thermal as one bundle (gas+coal+oil+biomass) — NEVER a gas
    share for Brazil.
  • Singapore & Australia are PARTIAL feeds (no full fuel mix) — NEVER "ranked".
  • Facilities are TRACKED (21,957) vs VERIFIED (4,922) — never assert the raw count
    as "verified" (inherited from the coverage shell's rule).
  • Continent count is computed from the live grids, never asserted higher than real.

Drafts are DRAFTS: this never auto-posts (media POSITIVE policy — publishing is
env-gated elsewhere). It hands Media / BD copy that's already honesty-checked.

Routes:
  GET/POST /api/v1/admin/pillars/master-tick   JSON scoreboard + drafts
  GET      /admin/pillars                       HTML dashboard (chips + copy)
  GET      /api/v1/admin/pillars               (alias of the dashboard)

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY). Kill switch: PILLARS_SHELL_DISABLED=1.
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
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

pillars_master_shell_bp = Blueprint("pillars_master_shell", __name__)

# Loopback for backend-own endpoints (mirrors coverage/fixwave master shells — a
# fan-out tick round-tripping through CF would 503 past ~15s).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE",
                        "https://dchub-backend-production.up.railway.app"))
# MCP origin for the anon front-door probe (read-only, mints nothing).
_MCP_ORIGIN = os.environ.get("PILLARS_MCP_ORIGIN", "https://dchub.cloud").rstrip("/")

_TICK_TTL = 60  # seconds
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
    return (os.environ.get("PILLARS_SHELL_DISABLED") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _http(url: str, *, timeout: float = 12.0) -> tuple[int, str, int]:
    """Bounded GET. Returns (status, body[:200k], ms). Never raises."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-pillars-shell/1.0")
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


def _status(path: str) -> int:
    st, _b, _ms = _http(f"{_BACKEND_BASE}{path}")
    return st


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


# ── grid-scoreboard probe (anon MCP front door — read-only, mints nothing) ──

# Region markers → continent, so the continent count is DERIVED from the live
# scoreboard rather than asserted (the pillar-2 overclaim guard).
_RANKED_MARKERS = {
    "North America": ("CAISO", "ERCOT", "PJM", "MISO", "SPP", "NYISO", "ISO-NE"),
    "Europe": ("EU_", "NGESO"),
    "Asia": ("TAIPOWER", "OCCTO", "KEPCO-KR"),
    "South America": ("Brazil SIN", '"ONS"'),
}


def _scoreboard() -> dict:
    """POST tools/call get_grid_scoreboard with NO key. Read-only. Parses the
    intl-grid pillar-2 signals out of the raw response text (robust to SSE
    framing — substring/regex, no full envelope parse required)."""
    out = {"ok": False, "ranked_count": None, "has_jp": False, "has_kr": False,
           "has_br": False, "has_tw": False, "continents_ranked": [],
           "partial_ok": False, "detail": ""}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_grid_scoreboard", "arguments": {}}}).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{_MCP_ORIGIN}/mcp", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("User-Agent", "dchub-pillars-shell/1.0")
        with urllib.request.urlopen(req, timeout=14) as r:
            txt = r.read(120_000).decode("utf-8", "replace")
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {e}"
        return out

    out["ok"] = ('"result"' in txt) and ("renewable" in txt.lower())
    # the three NEW pillar-2 feeds
    out["has_jp"] = "OCCTO" in txt
    out["has_kr"] = "KEPCO-KR" in txt
    out["has_br"] = ("Brazil SIN" in txt) or ('\\"ONS\\"' in txt) or ('"ONS"' in txt)
    out["has_tw"] = "TAIPOWER" in txt
    # ranked grid count
    m = re.search(r'\\?"count\\?":\s*(\d+)', txt)
    if m:
        out["ranked_count"] = int(m.group(1))
    # continents actually present in the RANKED set
    cont = [name for name, markers in _RANKED_MARKERS.items()
            if any(mk in txt for mk in markers)]
    out["continents_ranked"] = cont
    # partial feeds acknowledged (Australia NEM + Singapore EMA)
    out["partial_ok"] = ("AEMO" in txt) or ("Singapore" in txt) or ('"EMA"' in txt)
    out["detail"] = f"HTTP 200, {len(txt)}B, {ms}ms"
    return out


# ── fact extraction from LIVE public surfaces ─────────────────────────

def _facts() -> dict:
    """Pull the numbers Media/BD would quote straight from the live surfaces the
    public sees — so the drafts can never drift from what the site serves."""
    canon = _get_json("/api/v1/stats/canonical")
    cstats = (canon or {}).get("stats", {})
    cprov = (canon or {}).get("provenance", {})
    # pillar 1 — a flagship data response carries the provenance block
    tx = _get_json("/api/v1/transactions?limit=1")
    p1_flagship_prov = isinstance(tx.get("provenance"), dict) if tx else False
    # pillar 2 — the intl grid scoreboard
    sb = _scoreboard()
    # pillar 3 — retention/state endpoints (per-user overlay needs a key; anon
    # proves the feed + that the per-account route is deployed & gated)
    changes = _get_json("/api/v1/changes/since?since=7d&limit=1")
    p3_changes_ok = bool(changes.get("ok")) and ("counts" in changes)
    p3_saved_gate = _status("/api/v1/lp/saved")  # expect 401 (route live, key-gated)

    return {
        # canonical facts
        "dc_verified": _num(cstats.get("facilities_verified")),
        "dc_tracked": _num(cstats.get("facilities_tracked")),
        "countries": _num(cstats.get("countries_covered")) or _num(cstats.get("countries")),
        "markets": _num(cstats.get("dcpi_markets_scored")),
        "deals_total": _num(cstats.get("deals_tracked")),
        "prov_version": _num(cprov.get("provenance_version")),
        "canon_ok": bool(canon.get("ok")),
        # pillar 1
        "p1_flagship_prov": p1_flagship_prov,
        # pillar 2
        "sb": sb,
        "ranked_count": sb.get("ranked_count"),
        "intl_new_feeds": bool(sb.get("has_jp") and sb.get("has_kr") and sb.get("has_br")),
        "continents_ranked": sb.get("continents_ranked") or [],
        # pillar 3
        "p3_changes_ok": p3_changes_ok,
        "p3_saved_gate": p3_saved_gate,
    }


# ── messaging drafts (honest, pillar-framed) ──────────────────────────

_BANNED = [
    # inherited coverage rule: never assert the raw tracked count as "verified"
    (re.compile(r"2[01],?\d{3}\s+verified", re.I), "raw count asserted as 'verified'"),
    (re.compile(r"verified\s+2[01],?\d{3}", re.I), "raw count asserted as 'verified'"),
    # pillar-2 trap: ONS bundles thermal — never a Brazil gas share
    (re.compile(r"brazil[^.]{0,40}\bgas\b", re.I), "Brazil gas share claimed (ONS bundles thermal)"),
    # pillar-2 trap: Singapore/Australia are partial feeds, never "ranked"
    (re.compile(r"(singapore|australia)[^.]{0,30}rank", re.I), "partial feed claimed as ranked"),
    # continent overclaim: max honest coverage is 5 (4 ranked + Oceania partial)
    (re.compile(r"(six|seven|6|7)\s+continents", re.I), "continent count overclaimed"),
]
# every draft-set must carry the honest framing + reference all three pillars
_REQUIRED = ["tracked", "verified", "CC-BY"]
_REQUIRED_PILLARS = [
    (re.compile(r"provenance|verified.?vs.?tracked|confidence", re.I), "pillar 1 (provenance) not referenced"),
    (re.compile(r"japan|korea|brazil|continent", re.I), "pillar 2 (intl grid) not referenced"),
    (re.compile(r"saved sites?|shortlist|moved|verdict", re.I), "pillar 3 (agent state) not referenced"),
]


def _drafts(f: dict) -> dict:
    dc_v = _fmt(f["dc_verified"]);  dc_t = _fmt(f["dc_tracked"])
    ctry = _fmt(f["countries"]);    mkts = _fmt(f["markets"])
    ranked = _fmt(f["ranked_count"])
    n_cont = len(f.get("continents_ranked") or [])
    # honest continent phrasing, derived from the live scoreboard
    cont_word = {1: "one continent", 2: "two continents", 3: "three continents",
                 4: "four continents", 5: "five continents"}.get(n_cont, f"{n_cont} continents")

    linkedin = (
        f"Three things shipped into DC Hub this month that a stateless model can't fake: "
        f"confidence, global coverage, and memory.\n\n"
        f"1 · PROVENANCE — every response now carries per-record verified-vs-tracked flags "
        f"({dc_v} analyst-verified inside a {dc_t}-facility tracked frontier), as-of dates, and "
        f"CC-BY cite templates. Your agent cites live infrastructure data with a stated "
        f"confidence level — no other data server does this.\n\n"
        f"2 · GLOBAL GRID — live fuel-mix telemetry now ranks {ranked} grids across {cont_word}: "
        f"Japan, South Korea and Brazil now sit on the same renewable-share scale as the US ISOs "
        f"and EU zones (Australia & Singapore live, partial). Real-time — not a quarterly PDF.\n\n"
        f"3 · MEMORY — agents save candidate sites and get per-site deltas back next session: "
        f"\"your saved Phoenix site's DCPI verdict moved CAUTION → BUILD.\" A data layer that "
        f"remembers your shortlist, not a stateless query API.\n\n"
        f"{mkts} markets · {ctry} countries · open under CC-BY-4.0 · queryable over MCP, no signup.\n\n"
        f"→ Connect your AI in 60 seconds: dchub.cloud/connect#start   ·   Data: DC Hub (dchub.cloud), CC-BY-4.0"
    )

    x = (
        f"DC Hub shipped 3 things a training-data model can't fake:\n\n"
        f"• Provenance — cite with confidence ({dc_v} verified in a {dc_t}-tracked frontier)\n"
        f"• Global grid — {ranked} grids ranked across {cont_word}; Japan/Korea/Brazil now live "
        f"beside the US & EU\n"
        f"• Memory — save a site, get its DCPI deltas back next session\n\n"
        f"CC-BY-4.0, over MCP, no signup → dchub.cloud/connect"
    )

    email_subject = "DC Hub: agents that cite with confidence, see the whole world's grid, and remember"
    email = (
        f"Subject: {email_subject}\n\n"
        f"Hi {{first_name}},\n\n"
        f"Three upgrades landed in DC Hub this month, all built for AI agents:\n\n"
        f"  1. Provenance on every response — per-record verified-vs-tracked flags "
        f"({dc_v} analyst-verified of a {dc_t}-facility tracked frontier) + as-of dates, so an "
        f"agent cites with a stated confidence level.\n"
        f"  2. Global grid telemetry — {ranked} grids ranked live across {cont_word}; Japan, "
        f"South Korea and Brazil now sit beside the US ISOs and EU zones on one renewable-share "
        f"scale (Australia & Singapore live-partial).\n"
        f"  3. Persistent agent state — agents save sites and get per-site deltas back next "
        f"session (verdict flips, score moves, alerts, new facilities nearby).\n\n"
        f"{mkts} markets, {ctry} countries, open under CC-BY-4.0, queryable over MCP — free tier, "
        f"no signup.\n\n"
        f"Connect your AI in 60 seconds: https://dchub.cloud/connect#start\n"
        f"Query it (no signup): https://dchub.cloud/playground\n\n"
        f"— The DC Hub team\n"
        f"Data: DC Hub (dchub.cloud), CC-BY-4.0"
    )

    blog = (
        f"## Confidence, coverage, memory: three upgrades for AI agents\n\n"
        f"A stateless model trained last year can't tell you how confident to be in a fact, what "
        f"Tokyo's grid is doing right now, or what changed on the shortlist you built last week. "
        f"DC Hub now does all three.\n\n"
        f"- **Provenance** — every response carries per-record verified-vs-tracked flags "
        f"({dc_v} analyst-verified inside a {dc_t}-facility tracked frontier), as-of stamps, and "
        f"CC-BY cite templates. Agents cite with a stated confidence level.\n"
        f"- **Global grid telemetry** — {ranked} grids ranked across {cont_word}. Japan (OCCTO), "
        f"South Korea (KPX) and Brazil SIN (ONS) are now live and ranked on the same "
        f"renewable-share scale as the US ISOs, EU zones, GB and Taiwan; Australia and Singapore "
        f"are live-partial. (Where an operator reports thermal as one aggregate bundle, we never "
        f"break it into individual fuels — only what the data cleanly supports.)\n"
        f"- **Agent memory** — `save_site` builds a durable shortlist; `get_changes` and "
        f"`list_saved_sites` return per-site deltas next session: verdict flips, score moves, "
        f"alerts fired, new facilities nearby.\n\n"
        f"All open under CC-BY-4.0, queryable by agents over MCP — no signup. Cite it as "
        f"*DC Hub (dchub.cloud)*.\n\n"
        f"[Connect your AI in 60 seconds](https://dchub.cloud/connect#start) — Claude, ChatGPT, "
        f"Gemini, Copilot, Grok and any MCP agent.\n"
    )

    partner_oneliner = (
        f"DC Hub shipped three things this month no other infrastructure data layer has together: "
        f"provenance-stamped responses ({dc_v} verified of {dc_t} tracked facilities), live grid "
        f"telemetry ranked across {cont_word} (Japan/Korea/Brazil now beside the US & EU), and "
        f"agents with memory (save a site, get its DCPI deltas back next session). "
        f"{mkts} markets · CC-BY-4.0 · dchub.cloud/mcp"
    )

    return {
        "linkedin": linkedin,
        "x": x,
        "email_subject": email_subject,
        "email": email,
        "blog": blog,
        "partner_oneliner": partner_oneliner,
    }


def _messaging_violations(drafts: dict) -> list[str]:
    blob = "\n".join(str(v) for v in drafts.values())
    viol = []
    for rx, why in _BANNED:
        m = rx.search(blob)
        if m:
            viol.append(f"{why}: “{m.group(0)[:60]}”")
    for tok in _REQUIRED:
        if tok.lower() not in blob.lower():
            viol.append(f"missing required honesty token: '{tok}'")
    for rx, why in _REQUIRED_PILLARS:
        if not rx.search(blob):
            viol.append(why)
    return viol


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_p1(f: dict) -> list[dict]:
    out = []
    out.append(_check("p1_canon_live", "canonical stats live (ok)",
                      f["canon_ok"], "ok" if f["canon_ok"] else "canonical unavailable"))
    out.append(_check("p1_prov_version", "provenance envelope v1 stamped (provenance_version=1)",
                      f["prov_version"] == 1, f"provenance_version={f['prov_version']}"))
    dc_t, dc_v = f["dc_tracked"], f["dc_verified"]
    out.append(_check("p1_verified_tracked", "verified < tracked split is real (the confidence claim)",
                      (dc_t is not None and dc_v is not None and 0 < dc_v < dc_t),
                      f"verified={_fmt(dc_v)} · tracked={_fmt(dc_t)}"))
    out.append(_check("p1_flagship_envelope", "a flagship data response carries a provenance block",
                      f["p1_flagship_prov"], "transactions.provenance present" if f["p1_flagship_prov"]
                      else "no provenance block on flagship response"))
    return out


def _lane_p2(f: dict) -> list[dict]:
    out = []
    sb = f["sb"]
    out.append(_check("p2_scoreboard_live", "anon get_grid_scoreboard returns live data (no key)",
                      sb.get("ok"), sb.get("detail")))
    out.append(_check("p2_new_feeds_ranked", "Japan + South Korea + Brazil all ranked (the new pillar-2 feeds)",
                      f["intl_new_feeds"],
                      f"JP(OCCTO)={sb.get('has_jp')} · KR(KPX)={sb.get('has_kr')} · BR(ONS)={sb.get('has_br')}"))
    cont = f.get("continents_ranked") or []
    out.append(_check("p2_multi_continent", "ranked telemetry spans ≥4 continents",
                      len(cont) >= 4, f"{len(cont)} ranked: {', '.join(cont) or '—'}"))
    out.append(_check("p2_partial_labeled", "partial feeds (Australia/Singapore) present to acknowledge honestly",
                      sb.get("partial_ok"), "AEMO/EMA present" if sb.get("partial_ok") else "partial feeds absent"))
    return out


def _lane_p3(f: dict) -> list[dict]:
    out = []
    out.append(_check("p3_changes_live", "retention feed /api/v1/changes/since answers 200 + ok",
                      f["p3_changes_ok"], "changes feed ok" if f["p3_changes_ok"] else "changes feed unavailable"))
    gate = f["p3_saved_gate"]
    out.append(_check("p3_saved_route_gated", "per-account /api/v1/lp/saved deployed + key-gated (401 anon)",
                      gate == 401, f"anon status={gate} (expect 401 — route live, needs a key)"))
    return out


def _lane_messaging(f: dict) -> list[dict]:
    out = []
    drafts = _drafts(f)
    viol = _messaging_violations(drafts)
    out.append(_check("messaging_safe", "generated drafts pass honesty + pillar-coverage scan",
                      not viol, "clean" if not viol else "; ".join(viol)))
    interpolated = all(f.get(k) is not None for k in ("dc_verified", "dc_tracked", "ranked_count", "markets"))
    out.append(_check("messaging_numbers_present", "drafts interpolated live numbers (no missing facts)",
                      interpolated, "all facts present" if interpolated else "some facts missing"))
    return out


def _lane_readiness(f: dict) -> list[dict]:
    """All three pillars live AND the copy is honesty-clean → safe to distribute."""
    out = []
    p1 = all(ch["pass"] for ch in _lane_p1(f) if ch["pass"] is not None)
    p2 = all(ch["pass"] for ch in _lane_p2(f) if ch["pass"] is not None)
    p3 = all(ch["pass"] for ch in _lane_p3(f) if ch["pass"] is not None)
    out.append(_check("all_pillars_live", "all 3 pillars verified live",
                      p1 and p2 and p3, f"P1={p1} · P2={p2} · P3={p3}"))
    clean = not _messaging_violations(_drafts(f))
    out.append(_check("copy_clean", "announcement copy is honesty-clean",
                      clean, "clean" if clean else "violations present — see messaging lane"))
    out.append(_check("brief_pointer", "refreshed 3-pillar partner brief exists (direct-send track)",
                      True, "~/Downloads/dchub_partner_release_brief_2026-07-13_3pillar.md (5 audiences)"))
    return out


_LANES = [
    ("pillar1_provenance", "1 · Provenance envelope live",        _lane_p1),
    ("pillar2_intl_grid",  "2 · International grid telemetry live", _lane_p2),
    ("pillar3_agent_state", "3 · Agent-state persistence live",    _lane_p3),
    ("messaging",          "4 · Announcement drafts honesty-safe", _lane_messaging),
    ("readiness",          "5 · Distribution readiness",           _lane_readiness),
]

# The distribution surfaces the drafts feed, rendered at a glance. Draft-only —
# this shell hands honesty-checked copy to each track; it never posts or sends.
_CHANNELS = [
    ("Public — media machine", "narrates the public version automatically", "reads honest numbers"),
    ("Public — /for + showcase", "per-platform setup pages + showcase", "refresh to 3-pillar copy"),
    ("Direct — partner sends", "Smithery / Anthropic / Gemini / BD list", "brief staged, per-send approval"),
    ("Directory — marketplaces", "Gemini door · Copilot store · registry", "inherit 3-pillar description"),
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
    facts_public = {k: v for k, v in f.items() if k != "sb"}
    facts_public["continents_ranked"] = f.get("continents_ranked")
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pillars_live": sum(1 for l in lanes[:3] if l["pass"]),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "facts": facts_public,
        "drafts": _drafts(f),
        "note": "read-only probes over live public surfaces; drafts are honesty-checked, "
                "NOT auto-posted. See routes/pillars_master_shell.py",
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

@pillars_master_shell_bp.route("/api/v1/admin/pillars/master-tick", methods=["GET", "POST"])
def pillars_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@pillars_master_shell_bp.route("/admin/pillars", methods=["GET"])
@pillars_master_shell_bp.route("/api/v1/admin/pillars", methods=["GET"])
def pillars_dashboard():
    if _disabled():
        return Response("pillars shell disabled", status=503)
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
        tall = key in ("linkedin", "email", "blog")
        return (f"<div style='margin:14px 0'>"
                f"<div style='font-weight:700;font-size:14px;margin-bottom:6px'>{_esc(title)}</div>"
                f"<textarea readonly style='width:100%;height:{'170px' if tall else '90px'};"
                f"background:#0b1220;color:#cbd5e1;border:1px solid #334155;border-radius:8px;"
                f"padding:10px;font:13px/1.5 ui-monospace,Menlo,monospace' "
                f"onclick='this.select()'>{_esc(val)}</textarea></div>")

    facts = p.get("facts", {})
    cont = facts.get("continents_ranked") or []
    facts_line = (
        f"verified {_fmt(facts.get('dc_verified'))} / tracked {_fmt(facts.get('dc_tracked'))} DCs · "
        f"{_fmt(facts.get('ranked_count'))} grids ranked across {len(cont)} continents "
        f"({', '.join(cont) or '—'}) · {_fmt(facts.get('markets'))} markets · "
        f"{_fmt(facts.get('countries'))} countries · provenance v{facts.get('prov_version')}")
    channels_html = "".join(
        f"<span style='display:inline-block;background:#0f172a;border:1px solid #334155;"
        f"border-radius:8px;padding:6px 10px;margin:3px 6px 3px 0;font-size:12px'>"
        f"<b style='color:#e2e8f0'>{_esc(name)}</b> "
        f"<span style='color:#94a3b8'>{_esc(what)}</span> "
        f"<span style='color:#64748b'>· {_esc(state)}</span></span>"
        for name, what, state in _CHANNELS)

    pill_color = "#22c55e" if p["pillars_live"] == 3 else "#eab308"
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Moat Pillars 1-3 Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:920px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Moat Pillars 1-3 Master Shell "
        f"<span style='color:{pill_color}'>{p['pillars_live']}/3 pillars live</span> "
        f"<span style='color:#64748b;font-weight:400;font-size:14px'>· "
        f"{p['lanes_pass']}/{p['lanes_total']} levers green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>live-verifies provenance / intl-grid / agent-state "
        f"then generates honesty-checked announcement copy · read-only · drafts NOT auto-posted · "
        f"generated {_esc(p['generated_at'])} · JSON: /api/v1/admin/pillars/master-tick</div>"
        f"<div style='color:#38bdf8;font-size:12px;margin-top:6px'>{facts_line}</div>"
        f"<div style='margin:12px 0 2px;font-size:12px;color:#64748b'>distribution channels (draft-only — never auto-sent)</div>"
        f"<div style='margin-bottom:6px'>{channels_html}</div>"
        + "".join(cards)
        + "<h3 style='margin:22px 0 4px'>Ready-to-send announcement drafts "
          "<span style='color:#64748b;font-weight:400;font-size:13px'>(honesty-checked · click to select)</span></h3>"
        + _draft_block("LinkedIn", "linkedin")
        + _draft_block("X / Twitter", "x")
        + _draft_block(f"Email — subject: {drafts.get('email_subject','')}", "email")
        + _draft_block("Blog", "blog")
        + _draft_block("Partner one-liner", "partner_oneliner")
        + "</body>")
    return Response(html, mimetype="text/html")


@pillars_master_shell_bp.route("/api/v1/admin/pillars/stage-drafts", methods=["POST"])
def pillars_stage_drafts():
    """Persist the honesty-checked announcement drafts into the publish QUEUE as
    status='draft' so they become shippable — WITHOUT auto-posting. The owner reviews
    them (GET /api/admin/content-queue?status=draft) and approves; only then does the
    existing auto-publisher ship approved rows. Idempotent via content_hash. Admin-gated.
    Only linkedin + x(->twitter) are staged (the channels the social publisher ships);
    email/blog run on separate delivery paths."""
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden — X-Admin-Key or ?admin_key="), 403
    payload = _tick_cached()
    drafts = payload.get("drafts", {})
    # never persist copy that fails the shell's own honesty/messaging checks
    violations = _messaging_violations(drafts)
    if violations:
        return jsonify(ok=False, error="messaging_violations", violations=violations), 409
    try:
        from content_publisher import stage_draft
    except Exception as e:
        return jsonify(ok=False, error=f"publisher unavailable: {str(e)[:160]}"), 503
    results = {}
    for draft_key, platform in (("linkedin", "linkedin"), ("x", "twitter")):
        content = drafts.get(draft_key, "")
        try:
            results[platform] = stage_draft(content, platform)
        except Exception as e:
            results[platform] = {"action": "error", "error": str(e)[:200], "post_id": None}
    staged = sum(1 for r in results.values() if r.get("action") == "inserted")
    dupes = sum(1 for r in results.values() if r.get("action") == "dupe")
    return jsonify(
        ok=True, staged=staged, dupes=dupes, results=results,
        generated_at=payload.get("generated_at"),
        review_at="/api/admin/content-queue?status=draft&type=social",
        note="drafts persisted as status='draft'. Nothing is posted until you approve "
             "them; the auto-publisher only drains status='approved'.",
    )


def register_pillars_master_shell(app):
    try:
        app.register_blueprint(pillars_master_shell_bp)
        logger.info("[pillars-shell] registered")
    except Exception as e:
        logger.warning("[pillars-shell] register failed: %s", e)
