"""
competitive_seo.py — v1 "SEO + outreach competitive process".

2026-05-31. Owner mandate: FACTUAL, AGENT-FIRST. Turn the competitive moat
radar (routes/competitive_intel.py) into repeatable, *defensible* "DC Hub
vs <competitor>" SEO comparison pages + review-gated outreach drafts.

NON-NEGOTIABLE RULES (same spirit as competitive_intel.py):
  • Every competitor statement on these pages is OBSERVED-AND-DATED or
    explicitly "unknown". We PULL competitor specifics ONLY from
    competitive_intel's observed probe axes (llms_txt / mcp_server /
    machine_readable / public_api_hint) — each carries the date we saw it.
    We NEVER fabricate a capability, and we NEVER assert a competitor lacks
    one unless the radar actually observed a definitive signal (False).
  • NO PRICING. We never scrape, assert, or imply a competitor's price.
    (The owner's "90% cheaper" line is a SEPARATE, separately-verified
    step. This module omits pricing entirely — for us AND for them.)
  • NO DISPARAGEMENT. The only neutral framing line we allow is the public,
    defensible category already recorded in the radar registry (e.g.
    "subscription / login-gated platform") — and only when the category
    itself says so. Adjectives like "worse", "outdated", "expensive" never
    appear.
  • DC Hub's own edges are stated as TRUE FACTS sourced from canonical
    in-repo source-of-truth (canonical_stats.py for facility/market/grid
    counts; .well-known/mcp.json for the 25-tool count; routes/dcpi.py +
    routes/dcgi.py for the index names; routes/state_of_power.py for CC-BY).
  • NEVER 500, NEVER BLOCKS A WORKER. These are public pages on a
    1-replica backend (see the dchub backend-flapping note). We READ the
    radar's in-process probe cache only — we never probe a competitor host
    in the request path. A cold/stale entry renders DC Hub's edges plus a
    neutral "comparison refreshing" note and kicks a single background warm.
  • NO AUTO-SEND. The outreach endpoint returns DRAFTS labelled DRAFT for a
    human to review. It writes to NO send/publish/queue table.

Endpoints (blueprint competitive_seo_bp):
  GET  /vs                                  PUBLIC — SEO hub: lists every
                                            comparison page.
  GET  /vs/<slug>                           PUBLIC — factual "DC Hub vs
                                            <Competitor>" comparison landing
                                            page (HTML + JSON-LD + OG +
                                            canonical). Never 500s.
  GET  /api/v1/competitive/outreach-drafts  ADMIN — DRAFT outreach copy
                                            (partnerships / SEO) for human
                                            review. Never sends or queues.

Route ownership note (for the human registering this): /vs and /vs/<slug>
are ALSO declared today by routes/competitive_vs.py (the older hand-curated
generator, which contains unverified pricing) and /vs by
routes/quick_redirects.py (301 → /vs/dchawk). Flask resolves duplicate URL
rules by FIRST-REGISTERED-WINS, so to make THIS factual module own those
URLs, register competitive_seo_bp BEFORE those two blueprints (see the
main.py registration block in the hand-off report). This module reads no
proprietary data and writes nothing.
"""
from __future__ import annotations

import datetime
import html as _html
import json

from flask import Blueprint, Response, jsonify, request

competitive_seo_bp = Blueprint("competitive_seo", __name__)


# ──────────────────────────────────────────────────────────────────────
# Soft imports from the moat radar — this is the ONLY source of competitor
# facts. Everything is guarded so a radar import hiccup can never 500 a
# public page; we degrade to "DC Hub edges only + comparison refreshing".
# ──────────────────────────────────────────────────────────────────────

try:
    from routes.competitive_intel import (
        _COMPETITOR_BY_SLUG as _CI_BY_SLUG,
        _COMPETITORS as _CI_COMPETITORS,
        _DCHUB_DIFFERENTIATORS as _CI_DIFFERENTIATORS,
        _read_cached as _ci_read_cached,
        _kick_background_refresh as _ci_kick_refresh,
        _admin_guard as _ci_admin_guard,
    )
except Exception:  # pragma: no cover — defensive; radar should always import
    _CI_BY_SLUG = {}
    _CI_COMPETITORS = []
    _CI_DIFFERENTIATORS = []
    _ci_read_cached = None
    _ci_kick_refresh = None
    _ci_admin_guard = None


# ──────────────────────────────────────────────────────────────────────
# Public, SEO-friendly slug → radar slug.
#
# The radar uses internal slugs (dcbyte / dchawk / baxtel …). For clean,
# keyword-bearing URLs we expose human slugs (/vs/dc-byte,
# /vs/datacenterhawk, /vs/baxtel) and a few common aliases. EVERY public
# slug here maps to a real radar registry entry — we never invent a
# competitor the radar doesn't track. Aliases share one canonical public
# URL via a 301 to keep link-equity consolidated.
# ──────────────────────────────────────────────────────────────────────

# canonical public slug -> radar slug
_PUBLIC_TO_RADAR: dict[str, str] = {
    "dc-byte":          "dcbyte",
    "datacenterhawk":   "dchawk",
    "datacenterdynamics": "dcd",
    "data-center-frontier": "dcf",
    "datacenters-com":  "datacenters_com",
    "baxtel":           "baxtel",
}

# alias public slug -> canonical public slug (301 to consolidate equity)
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "dcbyte":        "dc-byte",
    "dc_byte":       "dc-byte",
    "dchawk":        "datacenterhawk",
    "dc-hawk":       "datacenterhawk",
    "datacenter-hawk": "datacenterhawk",
    "dcd":           "datacenterdynamics",
    "data-center-dynamics": "datacenterdynamics",
    "dcf":           "data-center-frontier",
    "datacenterfrontier": "data-center-frontier",
    "datacenters":   "datacenters-com",
    "datacenters.com": "datacenters-com",
    "datacenters_com": "datacenters-com",
}

# Public-facing display names — kept identical to the radar registry's
# display_name where it exists, so the two surfaces never disagree.
_PUBLIC_SLUG_ORDER = [
    "dc-byte", "datacenterhawk", "baxtel",
    "datacenterdynamics", "data-center-frontier", "datacenters-com",
]

# Neutral, defensible category framing. We ONLY state a "login/subscription"
# framing line for slugs whose public category genuinely is a paid/gated
# platform; for media/directory categories we use a plainly-true descriptor.
# These mirror the radar registry's public `category` field — no adjectives.
_NEUTRAL_FRAMING: dict[str, str] = {
    "dcbyte":          ("a paid data & maps platform (subscription / "
                        "login-gated access)"),
    "dchawk":          ("a paid data & maps platform (subscription / "
                        "login-gated access)"),
    "dcd":             "an industry media, research & events publisher",
    "dcf":             "an industry media & news publisher",
    "datacenters_com": "a data center directory",
    "baxtel":          "a data center directory & map",
}


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _today() -> str:
    return datetime.date.today().isoformat()


# ──────────────────────────────────────────────────────────────────────
# DC Hub verified edges — sourced from canonical_stats.py (live, with
# conservative floors) + the radar's _DCHUB_DIFFERENTIATORS (true facts).
# Everything here is defensible and dated by the page's "as of" line.
# ──────────────────────────────────────────────────────────────────────

def _dchub_numbers() -> dict:
    """Canonical, citation-safe DC Hub numbers. Never raises — falls back to
    the same conservative floors canonical_stats uses."""
    nums = {
        "facilities_phrase": "21,000+",
        "markets": 286,
        "grid_operators": 10,
        "utility_bas": 43,
        "grid_regions": 53,            # 10 NA operators + 43 utility BAs
        "mcp_tools": 25,               # .well-known/mcp.json tools array
    }
    try:
        from canonical_stats import get_canonical_stats, facilities_phrase
        s = get_canonical_stats()
        nums["facilities_phrase"] = facilities_phrase()
        if s.get("markets"):
            nums["markets"] = int(s["markets"])
        if s.get("grid_operators"):
            nums["grid_operators"] = int(s["grid_operators"])
        if s.get("utility_bas"):
            nums["utility_bas"] = int(s["utility_bas"])
        nums["grid_regions"] = nums["grid_operators"] + nums["utility_bas"]
    except Exception:
        pass
    return nums


def _dchub_edges() -> list[dict]:
    """DC Hub's verifiable edges, lead item first. Pulls the radar's
    differentiator facts when available (single source of truth), else a
    canonical fallback so the page is never empty."""
    if _CI_DIFFERENTIATORS:
        try:
            return [
                {"label": d["label"], "value": d["value"],
                 "proof": d.get("proof", "")}
                for d in _CI_DIFFERENTIATORS
            ]
        except Exception:
            pass
    n = _dchub_numbers()
    return [
        {"label": "Agent-native MCP server",
         "value": (f"Live streamable-HTTP MCP server with {n['mcp_tools']}+ "
                   "tools an AI agent can call directly — no scraping, no "
                   "PDF parsing, no login wall."),
         "proof": "https://dchub.cloud/mcp"},
        {"label": "Open, CC-BY-4.0 licensed data",
         "value": ("Core datasets and reports are published under CC-BY-4.0 "
                   "with stable URLs and JSON-LD — citable and reusable by "
                   "agents."),
         "proof": "https://dchub.cloud/state-of-power"},
        {"label": "Free self-serve tier",
         "value": ("A free tier (no credit card required) lets agents and "
                   "developers start querying immediately."),
         "proof": "https://dchub.cloud/signup"},
        {"label": "Comprehensive facility coverage",
         "value": (f"{n['facilities_phrase']} physical data center "
                   "facilities tracked with operator, location and power "
                   "detail."),
         "proof": "https://dchub.cloud/api/v1/facilities"},
    ]


# ──────────────────────────────────────────────────────────────────────
# Observed competitor axes — READ-ONLY from the radar's probe cache.
# We NEVER probe a competitor host in the request path (1-replica safety).
# Cold/stale → all-"unknown" + a neutral note, and a single background warm
# is kicked so the next render is hot.
# ──────────────────────────────────────────────────────────────────────

# Axis → human-readable "DC Hub has it" / "competitor observed" framing.
# `dchub` is a stated DC Hub fact; `present` / `absent` describe what the
# DATED observation of the competitor means — phrased neutrally, no spin.
_AXIS_META = {
    "mcp_server": {
        "label": "Agent-callable MCP server",
        "dchub": "Yes — live MCP server",
        "present": "MCP surface observed",
        "absent": "No MCP server on public surfaces",
        "unknown": "Not observed",
    },
    "llms_txt": {
        "label": "Published llms.txt for agents",
        "dchub": "Yes — /llms.txt + /llms-full.txt",
        "present": "llms.txt observed",
        "absent": "No llms.txt on public surfaces",
        "unknown": "Not observed",
    },
    "machine_readable": {
        "label": "Machine-readable structure (sitemap / JSON-LD)",
        "dchub": "Yes — sitemap.xml + JSON-LD",
        "present": "Machine-readable signals observed",
        "absent": "No sitemap / JSON-LD observed",
        "unknown": "Not observed",
    },
    "public_api_hint": {
        "label": "Public API hint",
        "dchub": "Yes — free REST + OpenAPI",
        "present": "Public API signals observed",
        "absent": "—",            # radar never asserts False here
        "unknown": "Not observed",
    },
}
_AXIS_ORDER = ["mcp_server", "llms_txt", "machine_readable", "public_api_hint"]


def _observe(radar_slug: str) -> tuple[dict | None, bool]:
    """Return (observation, is_fresh) READ-ONLY from the radar cache. Never
    raises, never hits the network. (None, False) if the radar is
    unavailable or the slug is unknown."""
    comp = _CI_BY_SLUG.get(radar_slug)
    if not comp or _ci_read_cached is None:
        return None, False
    try:
        obs, fresh = _ci_read_cached(comp)
        return obs, bool(fresh)
    except Exception:
        return None, False


def _kick_warm() -> None:
    """Best-effort single background probe warm so the next render is hot.
    Non-blocking; never raises."""
    if _ci_kick_refresh is None:
        return
    try:
        _ci_kick_refresh()
    except Exception:
        pass


def _capability_rows(obs: dict | None) -> list[dict]:
    """Build the capability table rows. Each competitor cell is strictly an
    OBSERVED-AND-DATED statement or 'Not observed' — never fabricated."""
    axes = (obs or {}).get("axes", {}) if obs else {}
    observed_at = (obs or {}).get("observed_at") if obs else None
    date_str = (observed_at or "")[:10] if observed_at else None
    rows = []
    for key in _AXIS_ORDER:
        meta = _AXIS_META[key]
        val = axes.get(key, "unknown")
        if val is True:
            comp_text = meta["present"]
            comp_state = "present"
        elif val is False and meta["absent"] != "—":
            # Definitive observation of absence — phrase as dated fact.
            comp_text = (f"{meta['absent']} (as of {date_str})"
                         if date_str else meta["absent"])
            comp_state = "absent"
        else:
            comp_text = meta["unknown"]
            comp_state = "unknown"
        rows.append({
            "label": meta["label"],
            "dchub": meta["dchub"],
            "competitor": comp_text,
            "competitor_state": comp_state,
        })
    return rows


# ──────────────────────────────────────────────────────────────────────
# Page model — assembled per public slug. Always returns a full model even
# when the radar is cold (DC Hub edges + "comparison refreshing").
# ──────────────────────────────────────────────────────────────────────

def _page_model(public_slug: str) -> dict | None:
    radar_slug = _PUBLIC_TO_RADAR.get(public_slug)
    if not radar_slug:
        return None
    comp = _CI_BY_SLUG.get(radar_slug) or {}
    display = comp.get("display_name") or radar_slug
    homepage = comp.get("homepage_url") or ""
    category = comp.get("category") or ""

    obs, fresh = _observe(radar_slug)
    if not fresh:
        _kick_warm()   # non-blocking; warms the radar cache for next time

    rows = _capability_rows(obs)
    observed_at = (obs or {}).get("observed_at") if obs else None
    note = (obs or {}).get("note") if obs else None

    # Did we observe ANY decisive competitor signal? Drives the "refreshing"
    # banner without ever fabricating a verdict.
    has_observation = bool(fresh) and any(
        r["competitor_state"] in ("present", "absent") for r in rows
    )

    return {
        "public_slug":   public_slug,
        "radar_slug":    radar_slug,
        "display_name":  display,
        "homepage_url":  homepage,
        "category":      category,
        "neutral_framing": _NEUTRAL_FRAMING.get(radar_slug, ""),
        "edges":         _dchub_edges(),
        "numbers":       _dchub_numbers(),
        "capability_rows": rows,
        "observed_at":   observed_at,
        "observation_note": note,
        "has_observation": has_observation,
        "canonical":     f"https://dchub.cloud/vs/{public_slug}",
        "as_of":         _today(),
    }


# ──────────────────────────────────────────────────────────────────────
# HTML rendering — dchub-brand styling, mirrors state_of_power.py /
# competitive_vs.py (Instrument Sans + JetBrains Mono + /static/dchub-brand.css).
# ──────────────────────────────────────────────────────────────────────

def _jsonld(m: dict) -> str:
    """JSON-LD for the comparison page: WebPage + ItemList of the edges, plus
    a FAQPage-free, fact-only structure. Competitor is referenced neutrally."""
    n = m["numbers"]
    edges_items = [
        {"@type": "ListItem", "position": i + 1, "name": e["label"]}
        for i, e in enumerate(m["edges"])
    ]
    obj = {
        "@context": "https://schema.org",
        "@type": ["WebPage", "TechArticle"],
        "name": f"DC Hub vs {m['display_name']}",
        "headline": f"DC Hub vs {m['display_name']}: an agent-native comparison",
        "description": (
            f"Factual comparison of DC Hub and {m['display_name']}. DC Hub is "
            f"the agent-native data-center intelligence platform: a live MCP "
            f"server ({n['mcp_tools']}+ tools), {n['facilities_phrase']} "
            f"facilities, open CC-BY-4.0 data, and live grid data across "
            f"{n['grid_operators']} North-American grid operators + "
            f"{n['utility_bas']} US utility balancing authorities."),
        "url": m["canonical"],
        "datePublished": _now_iso(),
        "dateModified": _now_iso(),
        "inLanguage": "en",
        "isAccessibleForFree": True,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "creator": {"@type": "Organization", "name": "DC Hub",
                    "url": "https://dchub.cloud"},
        "publisher": {"@type": "Organization", "name": "DC Hub",
                      "url": "https://dchub.cloud"},
        "about": {"@type": "Organization", "name": m["display_name"],
                  "url": m["homepage_url"]},
        "mainEntity": {
            "@type": "ItemList",
            "name": "DC Hub differentiators",
            "itemListElement": edges_items,
        },
        "keywords": [
            "data center intelligence", "DC Hub",
            f"{m['display_name']} alternative",
            "agent-native", "MCP server", "data center power",
            "DCPI", "DCGI", "site selection",
        ],
    }
    return json.dumps(obj, ensure_ascii=False)


def _render_html(m: dict) -> str:
    n = m["numbers"]
    disp = _html.escape(m["display_name"])
    disp_attr = disp.replace('"', "&quot;")

    # Lead edges (DC Hub's verifiable advantages) ------------------------
    edge_cards = "".join(
        f'<div class="edge"><div class="edge-h">{_html.escape(e["label"])}</div>'
        f'<p>{_html.escape(e["value"])}</p>'
        + (f'<a class="proof" href="{_html.escape(e["proof"])}">Verify &rarr;</a>'
           if e.get("proof") else "")
        + "</div>"
        for e in m["edges"]
    )

    # Capability table ---------------------------------------------------
    def _cell_class(state: str) -> str:
        return {"present": "comp-present", "absent": "comp-absent",
                "unknown": "comp-unknown"}.get(state, "comp-unknown")

    cap_rows = "".join(
        f"<tr><td><b>{_html.escape(r['label'])}</b></td>"
        f'<td class="dchub">{_html.escape(r["dchub"])}</td>'
        f'<td class="{_cell_class(r["competitor_state"])}">'
        f'{_html.escape(r["competitor"])}</td></tr>'
        for r in m["capability_rows"]
    )

    observed_line = ""
    if m.get("observed_at"):
        observed_line = (
            f'Competitor capabilities below were observed from public '
            f'surfaces as of {_html.escape((m["observed_at"] or "")[:10])}. '
            f'Cells read "Not observed" where no definitive public signal '
            f'was seen — never assumed.')
    else:
        observed_line = (
            'Competitor capability observations are refreshing — this page '
            'leads with DC Hub\'s verifiable edges in the meantime.')

    refreshing_banner = ""
    if not m.get("has_observation"):
        refreshing_banner = (
            '<div class="refreshing">Live competitor observations are '
            'refreshing. The DC Hub facts on this page are canonical and '
            'current; the side-by-side capability cells will populate from '
            'our dated public-surface probe shortly.</div>')

    framing = ""
    if m.get("neutral_framing"):
        framing = (
            f'<p class="framing">{disp} is publicly '
            f'{_html.escape(m["neutral_framing"])}. DC Hub takes the opposite '
            f'posture: open, agent-callable, and free to start.</p>')

    # Cross-links to the wider surface (task-required) -------------------
    other_links = " · ".join(
        f'<a href="/vs/{s}">{_html.escape(_disp_for_public(s))}</a>'
        for s in _PUBLIC_SLUG_ORDER if s != m["public_slug"]
    )

    grid_phrase = (f"{n['grid_operators']} North-American grid operators + "
                   f"{n['utility_bas']} US utility balancing authorities "
                   f"({n['grid_regions']} grid regions)")

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub vs {disp_attr} — Agent-Native Data Center Intelligence · DC Hub</title>
<meta name="description" content="DC Hub vs {disp_attr}: a factual, agent-first comparison. DC Hub is the agent-native data-center intelligence platform — live MCP server ({n['mcp_tools']}+ tools), {n['facilities_phrase']} facilities, open CC-BY-4.0 data, and live grid data across {n['grid_operators']} grid operators + {n['utility_bas']} utility BAs.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{m['canonical']}">
<meta property="og:title" content="DC Hub vs {disp_attr}">
<meta property="og:description" content="Agent-native vs the field. Live MCP server ({n['mcp_tools']}+ tools), {n['facilities_phrase']} facilities, open CC-BY-4.0 data, DCPI + DCGI. Factual, dated, no fluff.">
<meta property="og:type" content="article">
<meta property="og:url" content="{m['canonical']}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="DC Hub vs {disp_attr}">
<meta name="twitter:description" content="The agent-native data-center intelligence platform. Live MCP, open data, DCPI + DCGI.">
<script type="application/ld+json">{_jsonld(m)}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
  body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a12;color:#e6e9f0;margin:0;line-height:1.6}}
  .wrap{{max-width:1000px;margin:0 auto;padding:3rem 1.5rem 6rem}}
  .pill{{display:inline-block;padding:6px 14px;border-radius:99px;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.4);font-size:.74rem;color:#818cf8;font-weight:700;font-family:'JetBrains Mono',monospace;letter-spacing:.06em;margin-bottom:1rem;text-transform:uppercase}}
  h1{{font-size:2.6rem;font-weight:800;letter-spacing:-.025em;margin:0 0 .4rem;line-height:1.08}}
  h1 .vs{{color:#a855f7;margin:0 .45rem}}
  h2{{font-size:1.5rem;font-weight:800;margin:3rem 0 .8rem;letter-spacing:-.01em}}
  .sub{{color:#a1a1aa;font-size:1.05rem;margin:0 0 1.4rem;max-width:74ch}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:1.6rem 0 .5rem}}
  .stat{{background:rgba(255,255,255,.04);padding:16px 14px;border-radius:10px;border-left:3px solid #6366f1}}
  .stat-num{{font-size:1.7rem;font-weight:800;display:block;letter-spacing:-.02em}}
  .stat-lbl{{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;text-transform:uppercase;color:#94a3b8;margin-top:5px;letter-spacing:.06em}}
  .edges{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:1rem 0}}
  .edge{{background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:18px 20px}}
  .edge-h{{font-weight:700;color:#10b981;margin-bottom:.4rem;font-size:1.02rem}}
  .edge p{{margin:0 0 .6rem;color:#cbd5e1;font-size:.94rem}}
  .proof{{font-family:'JetBrains Mono',monospace;font-size:.78rem;color:#818cf8;text-decoration:none}}
  .proof:hover{{text-decoration:underline}}
  .framing{{color:#cbd5e1;background:rgba(255,255,255,.03);border-left:3px solid #a855f7;padding:14px 18px;border-radius:8px;margin:1rem 0 0}}
  .observed{{color:#94a3b8;font-size:.88rem;margin:.4rem 0 1rem}}
  .refreshing{{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.35);color:#fcd34d;border-radius:10px;padding:14px 18px;margin:1rem 0;font-size:.92rem}}
  table{{width:100%;border-collapse:collapse;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:12px;overflow:hidden;margin:.6rem 0 1.4rem}}
  th{{text-align:left;padding:12px 14px;background:rgba(255,255,255,.03);color:#94a3b8;font-size:.76rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700}}
  th.dchub{{color:#10b981}}th.comp{{color:#a855f7}}
  td{{padding:13px 14px;border-top:1px solid rgba(255,255,255,.05);vertical-align:top;font-size:.93rem}}
  td b{{color:#cbd5e1;font-weight:600}}
  td.dchub{{color:#10b981;font-weight:500}}
  td.comp-present{{color:#cbd5e1}}
  td.comp-absent{{color:#cbd5e1}}
  td.comp-unknown{{color:#71717a;font-style:italic}}
  .cta{{background:linear-gradient(135deg,rgba(99,102,241,.12),rgba(168,85,247,.12));border:1px solid rgba(129,140,248,.4);border-radius:12px;padding:1.4rem 1.6rem;margin:1.4rem 0}}
  .cta h3{{margin:0 0 .5rem;color:#c7d2fe;font-size:1.15rem}}
  .cta p{{margin:0 0 .8rem;color:#cbd5e1}}
  .cta a.btn{{display:inline-block;background:#6366f1;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:700;font-size:.92rem}}
  .links{{display:flex;flex-wrap:wrap;gap:10px;margin:.8rem 0 0}}
  .links a{{color:#818cf8;text-decoration:none;font-size:.92rem;border:1px solid rgba(129,140,248,.3);border-radius:8px;padding:7px 13px}}
  .links a:hover{{border-color:#818cf8}}
  .foot{{color:#71717a;font-size:.84rem;margin-top:2.4rem;border-top:1px solid rgba(255,255,255,.08);padding-top:1.2rem}}
  .foot a{{color:#818cf8;text-decoration:none}}
  @media (max-width:760px){{ h1{{font-size:2rem}} .stats{{grid-template-columns:1fr 1fr}} .edges{{grid-template-columns:1fr}} .wrap{{padding:2rem 1.1rem 4rem}} }}
</style></head><body>
<div class="wrap">
  <div class="pill">Factual · Agent-first · No pricing claims</div>
  <h1>DC Hub <span class="vs">vs</span> {disp}</h1>
  <p class="sub">DC Hub is the <strong>agent-native</strong> data-center intelligence platform: an AI agent can query it directly over a live MCP server, get real-time grid data and two daily indices, and cite any answer from open CC-BY-4.0 datasets. Here's how that compares with {disp}, stated as facts — not adjectives.</p>

  <div class="stats">
    <div class="stat"><span class="stat-num">{n['mcp_tools']}+</span><span class="stat-lbl">MCP tools (live)</span></div>
    <div class="stat"><span class="stat-num">{_html.escape(n['facilities_phrase'])}</span><span class="stat-lbl">Facilities tracked</span></div>
    <div class="stat"><span class="stat-num">{n['markets']}</span><span class="stat-lbl">Markets scored (DCPI)</span></div>
    <div class="stat"><span class="stat-num">{n['grid_regions']}</span><span class="stat-lbl">Grid regions (live)</span></div>
  </div>

  <h2>Where DC Hub is structurally different</h2>
  <div class="edges">{edge_cards}</div>
  {framing}

  <h2>Capability comparison</h2>
  <p class="observed">{observed_line}</p>
  {refreshing_banner}
  <table>
    <thead><tr><th>Capability</th><th class="dchub">DC Hub</th><th class="comp">{disp}</th></tr></thead>
    <tbody>{cap_rows}</tbody>
  </table>
  <p class="observed">DC Hub's grid coverage spans {_html.escape(grid_phrase)}, plus the proprietary DC Hub Power Index (DCPI) and DC Hub Gas Index (DCGI), recomputed daily. We never assert {disp}'s pricing.</p>

  <div class="cta">
    <h3>Point an AI agent at DC Hub</h3>
    <p>Free to start, no credit card. Connect the MCP server or hit the REST API and ask "where can I get power in 90 days?" — structured JSON back, citable under CC-BY-4.0.</p>
    <a class="btn" href="https://dchub.cloud/signup">Start free</a>
  </div>

  <div class="links">
    <a href="/api/v1/competitive/positioning">Full positioning</a>
    <a href="/api/v1/competitive/why-dchub">Why DC Hub</a>
    <a href="/dcpi">DCPI — Power Index</a>
    <a href="/dcgi">DCGI — Gas Index</a>
    <a href="/state-of-power">State of Data Center Power</a>
    <a href="/vs">All comparisons</a>
  </div>

  <p class="foot">
    Other comparisons: {other_links}<br>
    Methodology: competitor capabilities are observed from public surfaces and dated; absence is shown only where a definitive public signal was seen, otherwise "Not observed". DC Hub figures are canonical (<a href="/state-of-power">source</a>). We publish no pricing claims about other companies. ·
    DC Hub · <a href="/">dchub.cloud</a> · <a href="/llms.txt">llms.txt</a>
  </p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""


def _disp_for_public(public_slug: str) -> str:
    radar_slug = _PUBLIC_TO_RADAR.get(public_slug, "")
    comp = _CI_BY_SLUG.get(radar_slug) or {}
    return comp.get("display_name") or public_slug


# ──────────────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────────────

def _html_headers() -> dict:
    return {"Cache-Control": "public, max-age=900, s-maxage=3600"}


@competitive_seo_bp.route("/vs/<slug>", methods=["GET"], strict_slashes=False)
def vs_page(slug):
    """Factual 'DC Hub vs <Competitor>' comparison landing page. NEVER 500s:
    on any error we still render DC Hub's edges with a refreshing note."""
    try:
        s = (slug or "").strip().lower()

        # Alias → canonical public slug: 301 to consolidate link equity.
        if s in _ALIAS_TO_CANONICAL:
            from flask import redirect
            return redirect(f"/vs/{_ALIAS_TO_CANONICAL[s]}", code=301)

        if s not in _PUBLIC_TO_RADAR:
            # Unknown competitor → soft 404 that still routes the visitor to
            # the SEO hub (never a hard error page).
            return Response(_render_index(), mimetype="text/html",
                            status=404, headers={"Cache-Control":
                                                 "public, max-age=600"})

        model = _page_model(s)
        if not model:
            return Response(_render_index(), mimetype="text/html",
                            status=404, headers={"Cache-Control":
                                                 "public, max-age=600"})
        return Response(_render_html(model), mimetype="text/html",
                        headers=_html_headers())
    except Exception:
        # Absolute belt-and-suspenders: a minimal but valid DC Hub page.
        try:
            return Response(_render_fallback(slug), mimetype="text/html",
                            headers={"Cache-Control": "public, max-age=300"})
        except Exception:
            return Response("<!doctype html><title>DC Hub</title>"
                            "<p>DC Hub — the agent-native data-center "
                            "intelligence platform. Visit "
                            '<a href="https://dchub.cloud">dchub.cloud</a>.',
                            mimetype="text/html", status=200)


def _render_fallback(slug) -> str:
    """Last-resort page — DC Hub edges only, no competitor data, never 500."""
    n = _dchub_numbers()
    edges = "".join(f"<li><b>{_html.escape(e['label'])}</b>: "
                    f"{_html.escape(e['value'])}</li>" for e in _dchub_edges())
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>DC Hub — Agent-Native Data Center Intelligence</title>"
            f'<link rel="canonical" href="https://dchub.cloud/vs">'
            f"<link rel=stylesheet href='/static/dchub-brand.css'></head>"
            f"<body style='font-family:sans-serif;max-width:760px;margin:2rem auto;"
            f"padding:1rem;background:#0a0a12;color:#e6e9f0'>"
            f"<h1>DC Hub</h1><p>The agent-native data-center intelligence "
            f"platform: live MCP server ({n['mcp_tools']}+ tools), "
            f"{_html.escape(n['facilities_phrase'])} facilities, open "
            f"CC-BY-4.0 data, DCPI + DCGI. Comparison refreshing.</p>"
            f"<ul>{edges}</ul>"
            f"<p><a href='/vs' style='color:#818cf8'>All comparisons</a> · "
            f"<a href='/positioning' style='color:#818cf8'>Positioning</a></p>"
            f"</body></html>")


def _index_jsonld(items: list[dict]) -> str:
    return json.dumps({
        "@context": "https://schema.org",
        "@type": ["CollectionPage", "ItemList"],
        "name": "DC Hub vs the field — data center intelligence comparisons",
        "description": ("Factual, agent-first comparisons of DC Hub against "
                        "other data-center intelligence sources. Every "
                        "competitor capability is observed-and-dated."),
        "url": "https://dchub.cloud/vs",
        "isAccessibleForFree": True,
        "publisher": {"@type": "Organization", "name": "DC Hub",
                      "url": "https://dchub.cloud"},
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "name": f"DC Hub vs {it['display']}",
             "url": f"https://dchub.cloud/vs/{it['slug']}"}
            for i, it in enumerate(items)
        ],
    }, ensure_ascii=False)


def _render_index() -> str:
    n = _dchub_numbers()
    items = [{"slug": s, "display": _disp_for_public(s),
              "category": (_CI_BY_SLUG.get(_PUBLIC_TO_RADAR[s], {}) or {})
              .get("category", "")}
             for s in _PUBLIC_SLUG_ORDER if s in _PUBLIC_TO_RADAR]
    cards = "".join(
        f'<a class="card" href="/vs/{it["slug"]}">'
        f'<div class="card-h">DC Hub <span class="vs">vs</span> '
        f'{_html.escape(it["display"])}</div>'
        f'<p>{_html.escape(it["category"])}</p></a>'
        for it in items
    )
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub vs the field — Data Center Intelligence Comparisons · DC Hub</title>
<meta name="description" content="Factual, agent-first comparisons of DC Hub against the data-center intelligence field. Live MCP server ({n['mcp_tools']}+ tools), {n['facilities_phrase']} facilities, open CC-BY-4.0 data, DCPI + DCGI. Every competitor capability is observed and dated.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/vs">
<meta property="og:title" content="DC Hub vs the field">
<meta property="og:description" content="Agent-native data-center intelligence vs the field. Factual, dated comparisons — no pricing claims, no fluff.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://dchub.cloud/vs">
<script type="application/ld+json">{_index_jsonld(items)}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
  body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a12;color:#e6e9f0;margin:0;line-height:1.6}}
  .wrap{{max-width:980px;margin:0 auto;padding:3rem 1.5rem 6rem}}
  .pill{{display:inline-block;padding:6px 14px;border-radius:99px;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.4);font-size:.74rem;color:#818cf8;font-weight:700;font-family:'JetBrains Mono',monospace;letter-spacing:.06em;margin-bottom:1rem;text-transform:uppercase}}
  h1{{font-size:2.6rem;font-weight:800;letter-spacing:-.025em;margin:0 0 .4rem}}
  .sub{{color:#a1a1aa;font-size:1.05rem;margin:0 0 2rem;max-width:74ch}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin:1.4rem 0}}
  /* r47.46 (2026-05-27): display:block — /vs/* index renders `<a class="card">`
     with block-level <div class="card-h"> + <p> children. Without explicit
     display:block, default-inline anchors collapse their hit region in CSS
     grid. Same fix as r47.44 (/dcpi), r47.45 (/dcgi). text-decoration +
     color already set so visual is identical. */
  .card{{display:block;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px 22px;text-decoration:none;color:inherit;transition:.15s}}
  .card:hover{{border-color:#818cf8;transform:translateY(-2px)}}
  .card-h{{font-weight:800;font-size:1.2rem;margin-bottom:.3rem}}
  .card-h .vs{{color:#a855f7;margin:0 .35rem}}
  .card p{{margin:0;color:#a1a1aa;font-size:.88rem}}
  .links{{display:flex;flex-wrap:wrap;gap:10px;margin:1.6rem 0 0}}
  .links a{{color:#818cf8;text-decoration:none;font-size:.92rem;border:1px solid rgba(129,140,248,.3);border-radius:8px;padding:7px 13px}}
  .foot{{color:#71717a;font-size:.84rem;margin-top:2.4rem;border-top:1px solid rgba(255,255,255,.08);padding-top:1.2rem}}
  .foot a{{color:#818cf8;text-decoration:none}}
</style></head><body>
<div class="wrap">
  <div class="pill">Factual · Agent-first · No pricing claims</div>
  <h1>DC Hub vs the field</h1>
  <p class="sub">Factual, agent-first comparisons. DC Hub is the agent-native data-center intelligence platform — a live MCP server ({n['mcp_tools']}+ tools), {_html.escape(n['facilities_phrase'])} facilities, open CC-BY-4.0 data, and two daily indices (DCPI + DCGI). Every competitor capability below is observed from public surfaces and dated; we publish no pricing claims about other companies.</p>
  <div class="grid">{cards}</div>
  <div class="links">
    <a href="/api/v1/competitive/positioning">Full positioning</a>
    <a href="/api/v1/competitive/why-dchub">Why DC Hub</a>
    <a href="/dcpi">DCPI</a>
    <a href="/dcgi">DCGI</a>
    <a href="/state-of-power">State of Data Center Power</a>
  </div>
  <p class="foot">DC Hub · <a href="/">dchub.cloud</a> · <a href="/llms.txt">llms.txt</a> · <a href="/built-for-ai">Built for AI</a></p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""


@competitive_seo_bp.route("/vs", methods=["GET"], strict_slashes=False)
def vs_index():
    """SEO hub listing every comparison page. Never 500s."""
    try:
        return Response(_render_index(), mimetype="text/html",
                        headers=_html_headers())
    except Exception:
        return Response(_render_fallback(None), mimetype="text/html",
                        headers={"Cache-Control": "public, max-age=300"})


# ──────────────────────────────────────────────────────────────────────
# ADMIN: GET /api/v1/competitive/outreach-drafts
# Returns DRAFT outreach copy for a human (partnerships / SEO) to review and
# send MANUALLY. Writes to NO send/publish/queue. Reuses the radar's
# _admin_guard. Drafts contain NO unverified competitor claims and NO pricing.
# ──────────────────────────────────────────────────────────────────────

def _outreach_admin_guard():
    """Prefer the radar's _admin_guard (single source of the admin pattern);
    fall back to an inline equivalent if the import was unavailable."""
    if _ci_admin_guard is not None:
        return _ci_admin_guard()
    import os
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    if not expected or provided != expected:
        return jsonify({"ok": False, "error": "unauthorized",
                        "hint": "X-Admin-Key header required"}), 401
    return None


def _outreach_cors() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
        "Cache-Control": "no-store",
    }


def _build_outreach_drafts() -> list[dict]:
    """DRAFT outreach notes a partnerships/SEO person could send. Every line
    is a verifiable DC Hub fact or a neutral, non-pricing observation. No
    competitor is disparaged; no competitor pricing is asserted.

    Two flavors, both review-required:
      • agent_platform — to an AI-agent / MCP platform or registry: "list
        DC Hub; here's the live, citable, agent-callable surface."
      • publication — to a data-center publication / analyst: "cite DC Hub's
        open, dated, machine-readable data in your coverage."
    """
    n = _dchub_numbers()
    facts = (f"a live MCP server ({n['mcp_tools']}+ tools), "
             f"{n['facilities_phrase']} data-center facilities, live grid "
             f"data across {n['grid_operators']} North-American grid "
             f"operators + {n['utility_bas']} US utility balancing "
             f"authorities, and two proprietary daily indices (the DC Hub "
             f"Power Index and the DC Hub Gas Index)")

    drafts = [
        {
            "id": "draft_agent_platform_listing",
            "audience": "AI agent / MCP platform or registry",
            "intent": "Get DC Hub listed as an agent-callable data source.",
            "channel_hint": "email or platform submission form",
            "subject": "DC Hub — an agent-callable data-center intelligence source",
            "body": (
                "Hi {name},\n\n"
                "I run partnerships at DC Hub (dchub.cloud). We publish "
                "data-center intelligence in a form AI agents can use "
                "directly: " + facts + ".\n\n"
                "Concretely, an agent can connect to our streamable-HTTP MCP "
                "server at https://dchub.cloud/mcp (manifest at "
                "https://dchub.cloud/.well-known/mcp.json) and get structured "
                "JSON back — no scraping, no PDFs, no login wall. Core "
                "datasets are CC-BY-4.0 with stable URLs and JSON-LD, so "
                "answers are citable.\n\n"
                "If it's a fit for {platform}, we'd love to be listed. Happy "
                "to provide whatever metadata your submission process needs. "
                "What's the best path?\n\n"
                "Thanks,\n{sender}\npartnerships@dchub.cloud"
            ),
        },
        {
            "id": "draft_publication_cite",
            "audience": "Data-center publication / analyst / journalist",
            "intent": ("Offer DC Hub's open, dated data as a citable source "
                       "for their coverage."),
            "channel_hint": "email",
            "subject": "Open, citable data-center data for your coverage",
            "body": (
                "Hi {name},\n\n"
                "I'm reaching out from DC Hub (dchub.cloud). We maintain a "
                "live, open data-center intelligence dataset — "
                + facts + " — and publish it under CC-BY-4.0 with stable "
                "URLs and JSON-LD, so it's free to quote, chart, and "
                "republish with attribution.\n\n"
                "Our recurring 'State of Data Center Power' report "
                "(https://dchub.cloud/state-of-power) scores markets with "
                "live BUILD / AVOID verdicts and is refreshed daily, with a "
                "machine-readable JSON twin and a documented methodology. If "
                "you're covering AI build-out, power constraints, or site "
                "selection, you're welcome to cite any of it.\n\n"
                "Happy to walk through the data or pull a custom cut for a "
                "story. Would that be useful?\n\n"
                "Best,\n{sender}\npartnerships@dchub.cloud"
            ),
        },
        {
            "id": "draft_partnership_data_feed",
            "audience": ("Research / advisory firm that publishes "
                         "data-center reports"),
            "intent": ("Pure-upside offer: DC Hub's live data can feed their "
                       "narratives, citation-ready, no NDA."),
            "channel_hint": "email",
            "subject": "Live, citation-ready data-center data for your research",
            "body": (
                "Hi {name},\n\n"
                "DC Hub (dchub.cloud) publishes live data-center "
                "intelligence — " + facts + " — under CC-BY-4.0, with stable "
                "URLs and JSON-LD baked in.\n\n"
                "The pitch is simple and pure-upside: your research team can "
                "pull our live figures into your reports and client "
                "briefings, citation-ready, with no licensing review and no "
                "NDA. We keep the data fresh and dated; you keep the "
                "analyst voice. If useful, we can also expose a tailored "
                "feed for {firm}.\n\n"
                "Worth a short call to scope it?\n\n"
                "Best,\n{sender}\npartnerships@dchub.cloud"
            ),
        },
    ]
    return drafts


@competitive_seo_bp.route("/api/v1/competitive/outreach-drafts",
                          methods=["GET", "OPTIONS"])
def outreach_drafts():
    if request.method == "OPTIONS":
        return ("", 204, _outreach_cors())
    guard = _outreach_admin_guard()
    if guard is not None:
        return guard

    drafts = _build_outreach_drafts()
    payload = {
        "ok": True,
        "as_of": _now_iso(),
        "drafts": drafts,
        "placeholders": {
            "{name}": "recipient first name",
            "{sender}": "your name",
            "{platform}": "their platform / product name",
            "{firm}": "their firm name",
        },
        "note": (
            "DRAFTS ONLY — nothing here is sent, queued, or published. These "
            "are starting points for a human (partnerships / SEO) to edit and "
            "send manually. Every line is a verifiable DC Hub fact or a "
            "neutral observation; no competitor is named, characterised, or "
            "priced. Confirm the live tool/facility/market counts before "
            "sending if they may have changed."),
    }
    resp = jsonify(payload)
    for k, v in _outreach_cors().items():
        resp.headers[k] = v
    return resp, 200
