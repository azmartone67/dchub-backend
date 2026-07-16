"""
routes/radar.py — "Grid Transition Radar" daily briefing (backend-served).

A distinct analyst *publication* on dchub.cloud: one live data core rendered as
a new edition each day, in the voice of a different audience (4-day rotation),
tier-gated (full for paid, teaser for anon/agents).

Serving model (chosen 2026-07-16): dynamic, like /research. The frontend
_routes.json forwards /radar* to Railway; this blueprint renders on the fly and
caches the data core for the day. NO static files, always fresh.

Routes:
  GET /radar                     -> today's edition (tier-gated HTML)
  GET /radar/<slug>              -> a specific edition (capital|siteselect|agents|press)
  GET /radar/<slug>.json         -> machine-readable teaser feed (agents)

Brand note: /radar is intentionally NOT on the dchub brand system (it's a
publication with its own identity) — it is absent from
brain_consistency_radar.check_page_brand_uniformity's PAGES list, so that
detector never scans it. If the rotating sampler (check_page_brand_drift) ever
picks it up, add "/radar" to that sampler's skip set.

Registration (add near the other register_blueprint calls in main.py):
    from routes.radar import register_radar
    register_radar(app)
"""
from __future__ import annotations
import os, json, datetime as dt
from flask import Blueprint, request, Response, jsonify

from routes import radar_templates as T          # normalize + render + tier gating

try:
    from routes.tier_gate import _resolve_caller_tier
except Exception:                                 # pragma: no cover - defensive
    def _resolve_caller_tier():
        return ("FREE", {})

radar_bp = Blueprint("radar", __name__)
_PAGES_DIR = os.path.join(os.path.dirname(__file__), "radar_pages")

# 4-day rotation. Order == cycle slot (Nº 001..004).
EDITIONS = [
    {"slug": "capital",    "no": 1, "title": "Capital Allocators"},
    {"slug": "siteselect", "no": 2, "title": "Site Selection & Ops"},
    {"slug": "agents",     "no": 3, "title": "Agents & Developers"},
    {"slug": "press",      "no": 4, "title": "Infra Press"},
]
_BY_SLUG = {e["slug"]: e for e in EDITIONS}
PAID = {"DEVELOPER", "PRO", "ENTERPRISE"}         # everyone else -> teaser

# ── data core: fetched from THIS backend over loopback, shaped for normalize ──
_CORE_CACHE = {"date": None, "core": None}        # 1 pull/day, shared across requests

def _internal(path: str, timeout: int = 6) -> dict:
    """GET an internal endpoint over loopback; dchub- UA => full (untiered) data."""
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout,
                         headers={"User-Agent": "dchub-internal-radar/1.0",
                                  "X-Internal-Request": "1"})
        return (r.json() or {}) if r.status_code == 200 else {}
    except Exception:
        return {}

def _pull_core() -> dict:
    """Assemble the `core` dict radar_templates.normalize() expects.

    MAPPING SEAM — verify these field names against the live JSON of
    /api/v1/grid/intelligence/<ISO> on first run (the .get() fallbacks make it
    fail soft, not crash). Everything else in the pipeline is already verified.
    """
    today = dt.datetime.now(dt.timezone.utc)
    if _CORE_CACHE["date"] == today.date() and _CORE_CACHE["core"]:
        return _CORE_CACHE["core"]

    grids = []
    for iso in T.US_ISOS:
        d = _internal(f"/api/v1/grid/intelligence/{iso}")
        if not d:
            continue
        grids.append({
            "iso": iso,
            "renewable_share_pct": d.get("renewable_share_pct"),
            "interconnection_queue": {"queued_gw": d.get("queue_depth_gw") or d.get("queued_gw")},
            "dcpi_detail": {
                "avg_queue_wait_months": d.get("avg_time_to_power_months") or d.get("avg_queue_wait_months"),
                "avg_curtailment_pct":   d.get("curtailment_pct") or d.get("avg_curtailment_pct"),
                "build_markets":         d.get("build_markets", 0),
            },
        })
    # Headline "US interconnection queue" = the canonical all-US total from the
    # interconnection-queue snapshot (~1,737 GW) — NOT the sum of the 7 ISO
    # generation queues (~1,090). Fall back to the sum only if the endpoint is down.
    _snap = _internal("/api/v1/interconnection-queue/snapshot")
    us_q = (_snap.get("totals") or {}).get("queued_load_gw")
    if not us_q:
        us_q = round(sum((g["interconnection_queue"]["queued_gw"] or 0) for g in grids), 1)
    ash = _internal("/api/v1/grid/intelligence/PJM-DOM")
    markets = _internal("/api/v1/markets?region=us&sort=facilities&limit=10")

    core = {
        "retrieved_at": today.isoformat(timespec="seconds"),
        "depth": "full",
        "citation": {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"},
        "scoreboard": {"us_interconnection_queue_gw": us_q, "grids": grids},
        "ashburn": {"demand_mw": ash.get("demand_mw"),
                    "lmp_rt_usd_mwh": ash.get("lmp_rt_usd_mwh"),
                    "lmp_congestion_usd_mwh": ash.get("lmp_congestion_usd_mwh")},
        "markets": {"results": (markets.get("results") or markets.get("markets") or [])},
    }
    _CORE_CACHE.update(date=today.date(), core=core)
    return core

# ── tier + rendering ─────────────────────────────────────────────────────────
def _tier() -> str:
    try:
        name, _ = _resolve_caller_tier()
    except Exception:
        name = "FREE"
    return "full" if str(name).upper() in PAID else "tease"

_NAV = (
    '<div style="position:sticky;top:0;z-index:200;display:flex;gap:16px;align-items:center;'
    'background:#0C0F12;border-bottom:1px solid #242A30;padding:11px clamp(16px,4vw,40px);'
    'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;letter-spacing:.03em">'
    '<a href="/" style="color:#93A8FF;text-decoration:none;font-weight:700">DC&nbsp;Hub</a>'
    '<span style="color:#3A434C">/</span>'
    '<a href="/radar" style="color:#ECEFF2;text-decoration:none">Grid&nbsp;Radar</a>'
    '<a href="/research" style="color:#7E868D;text-decoration:none;margin-left:auto">Research</a>'
    '<a href="/whats-new" style="color:#7E868D;text-decoration:none">What\'s&nbsp;New</a>'
    '<a href="/playground" style="color:#7E868D;text-decoration:none">Playground</a>'
    '</div>'
)

def _render_edition(slug: str, tier: str) -> str:
    with open(os.path.join(_PAGES_DIR, f"{slug}.html"), encoding="utf-8") as f:
        body = T.render(f.read(), T.normalize(_pull_core()), tier)
    # frame the publication with a slim, navigable DC Hub bar (own identity below)
    return _NAV + body

def _today_slug() -> str:
    doy = dt.datetime.now(dt.timezone.utc).timetuple().tm_yday
    return EDITIONS[doy % len(EDITIONS)]["slug"]

def _teaser_json(slug: str) -> dict:
    core = _pull_core()
    data = T.normalize(core)
    ed = _BY_SLUG[slug]
    isos = {r["iso"]: r for r in data["isos"]}
    return {
        "edition": slug, "cycle_no": ed["no"], "title": ed["title"],
        "retrieved_at": data["retrieved_at"],
        "citation": core.get("citation", {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"}),
        "teaser_metrics": [
            {"label": "US interconnection queue", "value": data["us_queue_gw"], "unit": "GW"},
            {"label": "Ashburn time-to-power", "value": isos.get("PJM", {}).get("wait_mo"), "unit": "months"},
        ],
        "locked": {"positioning_book": True, "buildability_quadrant": True,
                   "full_ledger": True, "provenance_envelope": True},
        "unlock": {"why": "Full edition (all ISO rows, deep radar) is paid depth.",
                   "human_cta": "Upgrade or connect DC Hub to read the full brief.",
                   "free_path": "claim_free_key", "agent_next_tool": "unlock_more_data",
                   "playground": "https://dchub.cloud/playground"},
    }

# ── routes ───────────────────────────────────────────────────────────────────
@radar_bp.route("/radar")
def radar_today():
    return Response(_render_edition(_today_slug(), _tier()), mimetype="text/html")

@radar_bp.route("/radar/<slug>")
def radar_edition(slug: str):
    if slug.endswith(".json"):
        base = slug[:-5]
        if base not in _BY_SLUG:
            return jsonify({"error": "unknown edition", "editions": list(_BY_SLUG)}), 404
        return jsonify(_teaser_json(base))
    if slug not in _BY_SLUG:
        return jsonify({"error": "unknown edition", "editions": list(_BY_SLUG)}), 404
    return Response(_render_edition(slug, _tier()), mimetype="text/html")


def register_radar(app):
    app.register_blueprint(radar_bp)
