"""
competitive_intel.py — Competitive Moat Radar + Agent-Conversion module.
2026-05-31.

Mandate: FACTUAL, AGENT-FIRST. Make DC Hub the obvious choice for AI
agents / MCP clients by surfacing DC Hub's *verifiable* strengths and
competitors' *observed* agent-readiness gaps — NEVER fabricated claims.

Design principles (non-negotiable):
  • Every competitor statement is OBSERVED-AND-DATED or "unknown".
    We only record `false` for an axis when we actually saw a 200/404
    that proves absence; otherwise the axis is "unknown". We never guess.
  • We NEVER scrape or assert pricing. (The owner mentioned "90% cheaper"
    — that is a SEPARATE, later, publicly-verified step. This module
    omits pricing entirely.)
  • Probes are RESPECTFUL: one GET per surface, ≤6s per-host timeout,
    in-process cached with a long TTL (~6h) so we never hammer a host.
    A failing probe NEVER raises and NEVER 500s the endpoint.
  • DC Hub differentiators are stated as TRUE FACTS drawn from real,
    documented capabilities (see _DCHUB_DIFFERENTIATORS provenance notes).

Endpoints:
  GET  /api/v1/competitive/positioning   PUBLIC — dchub facts + observed
                                          competitor agent-readiness axes.
  GET  /api/v1/competitive/why-dchub     PUBLIC — positive, factual,
                                          agent-oriented pitch (no
                                          competitor names) for embedding
                                          in llms.txt / agent-broadcast.
  GET  /api/v1/competitive/media-drafts  ADMIN — 2-3 DRAFT educational
                                          post texts for MANUAL review.
                                          Does NOT publish or queue.

This module mirrors routes/agent_broadcast.py for its guarded / never-500 /
CORS / Cache-Control style and routes/brain_v2_layer5.py for the
_admin_guard() pattern. It reads no proprietary data and writes nothing.

Real DC Hub numbers used here are sourced from in-repo source-of-truth
files (.well-known/mcp.json for the live tool count; routes/
agent_capabilities_feed.py for facility / market counts; routes/dcpi.py
and routes/dcgi.py for the index names). Where a value is documented but
not derivable from a single canonical integer, a CONSERVATIVE documented
value is used and the provenance is noted inline.
"""
from __future__ import annotations

import datetime
import threading
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request


competitive_intel_bp = Blueprint("competitive_intel", __name__)


# ──────────────────────────────────────────────────────────────────────
# 1. Competitor registry — STATIC, FACTUAL public profiles only.
#    slug / display_name / homepage_url / category are public facts.
#    No claims, no pricing, no observed axes here — those come from
#    probe_competitor() and are always dated.
# ──────────────────────────────────────────────────────────────────────

_COMPETITORS: list[dict] = [
    {
        "slug":         "dcbyte",
        "display_name": "DC Byte",
        "homepage_url": "https://www.dcbyte.com",
        "category":     "Paid data & maps",
    },
    {
        "slug":         "dchawk",
        "display_name": "DC Hawk",
        "homepage_url": "https://www.dchawk.com",
        "category":     "Paid data & maps",
    },
    {
        "slug":         "dcd",
        "display_name": "DatacenterDynamics",
        "homepage_url": "https://www.datacenterdynamics.com",
        "category":     "Media, research & events",
    },
    {
        "slug":         "dcf",
        "display_name": "Data Center Frontier",
        "homepage_url": "https://www.datacenterfrontier.com",
        "category":     "Media & news",
    },
    {
        "slug":         "datacenters_com",
        "display_name": "datacenters.com",
        "homepage_url": "https://www.datacenters.com",
        "category":     "Directory",
    },
    {
        "slug":         "baxtel",
        "display_name": "Baxtel",
        "homepage_url": "https://baxtel.com",
        "category":     "Directory & map",
    },
]

_COMPETITOR_BY_SLUG: dict[str, dict] = {c["slug"]: c for c in _COMPETITORS}


# ──────────────────────────────────────────────────────────────────────
# 2. DC Hub differentiators — STATED TRUE FACTS from real capabilities.
#    Each entry: key, label, value (the factual claim), proof (a live
#    URL an agent can verify), and source (in-repo provenance for the
#    number, for our own auditability — not shown as a claim).
#
#    Number provenance (verified in-repo on 2026-05-31):
#      • mcp tool count = 25  → /.well-known/mcp.json `tools` array has
#        exactly 25 entries (canonical manifest; marketing copy elsewhere
#        says 28/29 but the manifest is the source of truth, so we cite
#        the conservative manifest count and call it "25+").
#      • facilities ~21,000+ → routes/agent_capabilities_feed.py default
#        counts.facilities = 21000 (live-overridden from
#        discovered_facilities); also routes/agent_a2a.py "21,000+".
#      • markets scored ~286 → routes/agent_capabilities_feed.py default
#        counts.markets_scored = 286 (live-overridden from
#        market_power_scores). We state "~285+" conservatively.
#      • grids with live data = 11 US ISOs/BAs + 3 international = 14.
#        routes/agent_capabilities_feed.py us_isos (PJM,CAISO,ERCOT,MISO,
#        SPP,NYISO,ISO-NE,TVA,SOCO,FRCC,BPA) + international_isos
#        (AESO, Hydro-Québec, Nord Pool). We state the VERIFIABLE count;
#        we deliberately do NOT claim "51 grids" because no in-repo
#        source supports it.
#      • DCPI = "DC Hub Power Index" (routes/dcpi.py), DCGI = "Data
#        Center Gas Index" (routes/dcgi.py) — both proprietary live
#        indices computed daily.
#      • CC-BY-4.0 open data → routes/state_of_power.py + manifest license.
#      • Free tier (100 req/day, no credit card) → llms.txt API Access.
# ──────────────────────────────────────────────────────────────────────

_DCHUB_DIFFERENTIATORS: list[dict] = [
    {
        "key":    "agent_native_mcp",
        "label":  "Agent-native MCP server",
        "value":  ("Live streamable-HTTP MCP server with 25+ tools an AI "
                   "agent can call directly — no scraping, no PDF parsing."),
        "proof":  "https://dchub.cloud/mcp",
        "source": ".well-known/mcp.json tools array (25)",
    },
    {
        "key":    "open_cc_by_data",
        "label":  "Open, CC-BY-4.0 licensed data",
        "value":  ("Core datasets and reports are published under "
                   "CC-BY-4.0 with stable URLs and JSON-LD — citable and "
                   "reusable by agents, not login-walled."),
        "proof":  "https://dchub.cloud/state-of-power",
        "source": "routes/state_of_power.py (CC-BY-4.0 + JSON-LD)",
    },
    {
        "key":    "live_grid_data",
        "label":  "Live grid & energy data",
        "value":  ("Real-time grid data across 11 US ISOs / balancing "
                   "authorities (PJM, CAISO, ERCOT, MISO, SPP, NYISO, "
                   "ISO-NE, TVA, SOCO, FRCC, BPA) plus 3 international "
                   "grids (AESO, Hydro-Québec, Nord Pool)."),
        "proof":  "https://dchub.cloud/api/v1/reports/state-of-power",
        "source": "routes/agent_capabilities_feed.py us_isos + international_isos",
    },
    {
        "key":    "proprietary_indices",
        "label":  "Proprietary live indices (DCPI + DCGI)",
        "value":  ("Two proprietary indices recomputed daily: the DC Hub "
                   "Power Index (DCPI) scores ~285+ markets with live "
                   "BUILD / AVOID verdicts, and the DC Hub Gas Index "
                   "(DCGI) scores gas access and cost by state."),
        "proof":  "https://dchub.cloud/dcpi",
        "source": "routes/dcpi.py (DCPI) + routes/dcgi.py (DCGI)",
    },
    {
        "key":    "facilities",
        "label":  "Comprehensive facility coverage",
        "value":  ("21,000+ physical data center facilities tracked with "
                   "operator, location and power detail."),
        "proof":  "https://dchub.cloud/api/v1/facilities",
        "source": "routes/agent_capabilities_feed.py counts.facilities (21000)",
    },
    {
        "key":    "free_tier",
        "label":  "Free self-serve tier",
        "value":  ("A free tier (no credit card required) lets agents and "
                   "developers start querying immediately."),
        "proof":  "https://dchub.cloud/signup",
        "source": "llms.txt API Access (free tier, 100 req/day)",
    },
]


# ──────────────────────────────────────────────────────────────────────
# 3. Competitor probing — RESILIENT + RESPECTFUL.
#
#    Each axis value is strictly one of: True / False / "unknown".
#    We prefer "unknown" over a guessed False. We only record False when
#    we actually observed a definitive signal (e.g. a 404 / a 200 that is
#    HTML when a machine format was required). A probe failing NEVER
#    raises — the worst case is all-"unknown" axes with a note.
#
#    Caching: results are cached in-process for _PROBE_TTL_SECONDS (~6h)
#    keyed by slug, so we make at most one round of GETs per host per TTL
#    window across the whole worker. This is the "respectful" guarantee.
# ──────────────────────────────────────────────────────────────────────

_PROBE_TIMEOUT_SECONDS = 4          # per-surface hard cap (background-only)
_PROBE_TTL_SECONDS = 21600          # 6h — never hammer a competitor host
_PROBE_UA = ("DCHubCompetitiveIntel/1.0 (+https://dchub.cloud; "
             "respectful one-shot agent-readiness probe)")

# slug -> {"observed_at": iso, "result": {...}}
_PROBE_CACHE: dict[str, dict] = {}
_PROBE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _http_get(url: str) -> dict:
    """Single, time-capped GET. Returns a small dict describing what we
    actually saw — never raises. On any failure, reachable=False.

    Returns: {reachable, status, content_type, body_snip}
      • reachable False  → host/path errored or timed out (→ "unknown")
      • status 200/404…  → an OBSERVED HTTP status (lets us record facts)
      • body_snip        → first ~4KB lowercased, for substring checks
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": _PROBE_UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(
            req, timeout=_PROBE_TIMEOUT_SECONDS
        ) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            try:
                raw = resp.read(4096) or b""
            except Exception:
                raw = b""
            try:
                body = raw.decode("utf-8", "ignore").lower()
            except Exception:
                body = ""
            return {
                "reachable":    True,
                "status":       int(status) if status else None,
                "content_type": ctype,
                "body_snip":    body,
            }
    except urllib.error.HTTPError as e:
        # An HTTPError is still an OBSERVED status (e.g. 404) — useful:
        # it lets us record a definitive False instead of "unknown".
        ctype = ""
        try:
            ctype = (e.headers.get("Content-Type") or "").lower()
        except Exception:
            pass
        return {
            "reachable":    True,
            "status":       int(getattr(e, "code", 0)) or None,
            "content_type": ctype,
            "body_snip":    "",
        }
    except Exception:
        # Timeout, DNS failure, TLS error, connection reset, anything.
        return {
            "reachable":    False,
            "status":       None,
            "content_type": "",
            "body_snip":    "",
        }


def _axis_llms_txt(homepage: str) -> tuple:
    """True  → /llms.txt returns 200 AND content-type is not text/html.
    False → /llms.txt returned a definitive 404 (proven absent).
    "unknown" → unreachable, or 200-but-HTML (a soft-404 / SPA fallback we
                can't trust as a real llms.txt).
    Returns (value, source_url)."""
    url = homepage.rstrip("/") + "/llms.txt"
    r = _http_get(url)
    if not r["reachable"]:
        return "unknown", url
    status = r["status"]
    if status == 200:
        ctype = r["content_type"]
        # A real llms.txt is text/plain (or markdown), not an HTML page.
        if ctype and "text/html" not in ctype:
            return True, url
        # 200 but HTML → almost certainly a catch-all/SPA fallback, not a
        # genuine llms.txt. We will not assert False (the file *might*
        # exist but be misserved); record "unknown".
        return "unknown", url
    if status == 404:
        return False, url           # proven absent
    return "unknown", url           # other status → can't tell


def _axis_mcp_server(homepage: str) -> tuple:
    """True  → any MCP discovery surface returns a non-404 signal.
    False → every probed MCP surface returned a definitive 404.
    "unknown" → none reachable (can't tell).
    Returns (value, source_url) where source_url is the surface that
    produced the decisive signal (or the first probed)."""
    base = homepage.rstrip("/")
    surfaces = [
        base + "/.well-known/mcp-protocol",
        base + "/.well-known/oauth-protected-resource/mcp",
        base + "/mcp",
    ]
    saw_404 = False
    first_url = surfaces[0]
    for url in surfaces:
        r = _http_get(url)
        if not r["reachable"]:
            continue
        status = r["status"]
        if status is None:
            continue
        if status != 404:
            # Any non-404 observed signal (200, 401, 405, 406, 5xx…) is a
            # positive hint that *something* MCP-ish answers here.
            return True, url
        saw_404 = True
    if saw_404:
        # We reached at least one surface and every reached surface 404'd.
        return False, first_url
    return "unknown", first_url     # nothing reachable


def _axis_machine_readable(homepage: str) -> tuple:
    """True  → /sitemap.xml is present (200) OR homepage embeds JSON-LD
              (<script type="application/ld+json">).
    False → /sitemap.xml returned a definitive 404 AND homepage was
              reachable with no JSON-LD detected.
    "unknown" → couldn't reach enough to tell.
    Returns (value, source_url)."""
    base = homepage.rstrip("/")
    sitemap_url = base + "/sitemap.xml"
    sm = _http_get(sitemap_url)
    if sm["reachable"] and sm["status"] == 200:
        return True, sitemap_url

    # No usable sitemap signal → look for JSON-LD on the homepage.
    hp = _http_get(homepage)
    if hp["reachable"] and hp["status"] == 200:
        body = hp["body_snip"]
        if "application/ld+json" in body or "schema.org" in body:
            return True, homepage
        # Homepage reachable, no JSON-LD in first 4KB. Only assert False
        # if the sitemap was DEFINITIVELY absent (404); otherwise the page
        # may carry JSON-LD below our 4KB read window → "unknown".
        if sm["reachable"] and sm["status"] == 404:
            return False, homepage
        return "unknown", homepage
    # Homepage unreachable → can't tell.
    return "unknown", sitemap_url


def _axis_public_api_hint(homepage: str) -> tuple:
    """Conservative: True only if homepage / /api / /docs visibly mentions
    an API. We never assert False here (a public API can exist without the
    word "api" in the first 4KB of these pages) — absence → "unknown".
    Returns (value, source_url)."""
    base = homepage.rstrip("/")
    candidates = [homepage, base + "/api", base + "/docs"]
    reached_any = False
    first_url = homepage
    for url in candidates:
        r = _http_get(url)
        if not r["reachable"]:
            continue
        reached_any = True
        if r["status"] == 200 and r["body_snip"]:
            body = r["body_snip"]
            # Conservative token set; "api" alone is too noisy, so require
            # an API-ish phrase or a developer surface that 200s as /api or
            # /docs (a 200 on /api or /docs is itself a strong hint).
            if ("/api" in url or "/docs" in url):
                return True, url
            for token in ("rest api", "developer api", "api documentation",
                          "api docs", "api access", "public api",
                          "api key", "api endpoint", "openapi", "swagger"):
                if token in body:
                    return True, url
    # We never claim "no API" from a homepage skim → unknown either way.
    return ("unknown", first_url) if reached_any else ("unknown", first_url)


def _probe_uncached(comp: dict) -> dict:
    """Run all four axis probes for one competitor. Fully guarded: any
    unexpected error collapses to all-"unknown" with an explanatory note.
    Returns the `result` block (without caching metadata)."""
    homepage = comp["homepage_url"]
    note = ""
    try:
        llms_v, llms_src = _axis_llms_txt(homepage)
        mcp_v, mcp_src = _axis_mcp_server(homepage)
        mr_v, mr_src = _axis_machine_readable(homepage)
        api_v, api_src = _axis_public_api_hint(homepage)
        axes = {
            "llms_txt":         llms_v,
            "mcp_server":       mcp_v,
            "machine_readable": mr_v,
            "public_api_hint":  api_v,
        }
        # If literally nothing was observable, say so plainly.
        if all(v == "unknown" for v in axes.values()):
            note = ("Host unreachable or returned no decisive signal at "
                    "probe time; all axes recorded as unknown.")
        else:
            note = ("Axes observed from public surfaces; 'false' is "
                    "recorded only where a definitive 200/404 was seen, "
                    "else 'unknown'. Pricing is never probed or asserted.")
        return {
            "axes":        axes,
            "axis_sources": {
                "llms_txt":         llms_src,
                "mcp_server":       mcp_src,
                "machine_readable": mr_src,
                "public_api_hint":  api_src,
            },
            "source_url":  homepage,
            "note":        note,
        }
    except Exception:
        # Belt-and-suspenders: a probe must NEVER raise to the caller.
        return {
            "axes": {
                "llms_txt":         "unknown",
                "mcp_server":       "unknown",
                "machine_readable": "unknown",
                "public_api_hint":  "unknown",
            },
            "axis_sources": {},
            "source_url":   homepage,
            "note":         ("Probe error; all axes recorded as unknown "
                             "(never fabricated). Pricing is never "
                             "probed or asserted."),
        }


def probe_competitor(slug: str, force: bool = False) -> dict | None:
    """Public entry point. Returns a dated observation block for one
    competitor (or None for an unknown slug). RESILIENT + RESPECTFUL:

      • In-process cached for ~6h per slug, so repeated /positioning
        calls never re-hit the competitor host. Pass force=True only for
        a deliberate refresh (still guarded, still never raises).
      • Never raises; on any failure the axes are "unknown" with a note.

    Shape:
      {
        "slug", "display_name", "category",
        "axes": {llms_txt, mcp_server, machine_readable, public_api_hint},
        "axis_sources": {...},      # which URL produced each signal
        "observed_at": <ISO>,       # when we actually probed
        "source_url":  <homepage>,
        "note": "<plain-English caveat>"
      }
    """
    comp = _COMPETITOR_BY_SLUG.get(slug)
    if not comp:
        return None

    if not force:
        cached = _PROBE_CACHE.get(slug)
        if cached:
            age = (datetime.datetime.utcnow()
                   - cached["_cached_at"]).total_seconds()
            if age < _PROBE_TTL_SECONDS:
                return _shape_observation(comp, cached["result"],
                                          cached["observed_at"])

    # Cold (or forced) — do the actual network work, guarded.
    try:
        result = _probe_uncached(comp)
    except Exception:
        result = {
            "axes": {k: "unknown" for k in
                     ("llms_txt", "mcp_server",
                      "machine_readable", "public_api_hint")},
            "axis_sources": {},
            "source_url":   comp["homepage_url"],
            "note":         ("Probe error; axes unknown (never "
                             "fabricated)."),
        }
    observed_at = _now_iso()
    with _PROBE_LOCK:
        _PROBE_CACHE[slug] = {
            "result":     result,
            "observed_at": observed_at,
            "_cached_at": datetime.datetime.utcnow(),
        }
    return _shape_observation(comp, result, observed_at)


def _shape_observation(comp: dict, result: dict, observed_at: str) -> dict:
    return {
        "slug":         comp["slug"],
        "display_name": comp["display_name"],
        "category":     comp["category"],
        "axes":         result.get("axes", {}),
        "axis_sources": result.get("axis_sources", {}),
        "observed_at":  observed_at,
        "source_url":   result.get("source_url", comp["homepage_url"]),
        "note":         result.get("note", ""),
    }


# ── Non-blocking refresh: probes run in a BACKGROUND thread, NEVER in the
#    request path. The 1-replica backend must never block a worker on 6
#    competitors' external sites (that was a 20s hang -> CF 503). /positioning
#    reads the cache only; cold/stale entries return "unknown (refreshing)"
#    and trigger one background warm, so the next request is hot. The 24x7
#    cron hits /refresh to keep it warm. ─────────────────────────────────
_REFRESH_RUNNING = False
_REFRESH_FLAG_LOCK = threading.Lock()


def _refresh_all_blocking():
    """Probe every competitor (force) to warm _PROBE_CACHE. Runs ONLY in a
    daemon thread — never call from a request thread."""
    global _REFRESH_RUNNING
    try:
        for comp in _COMPETITORS:
            try:
                probe_competitor(comp["slug"], force=True)
            except Exception:
                pass
    finally:
        with _REFRESH_FLAG_LOCK:
            _REFRESH_RUNNING = False


def _kick_background_refresh() -> bool:
    """Start a single background warm if one isn't already in flight.
    Returns True if started, False if one was already running. Non-blocking."""
    global _REFRESH_RUNNING
    with _REFRESH_FLAG_LOCK:
        if _REFRESH_RUNNING:
            return False
        _REFRESH_RUNNING = True
    try:
        threading.Thread(target=_refresh_all_blocking,
                         name="competitive-probe-refresh",
                         daemon=True).start()
        return True
    except Exception:
        with _REFRESH_FLAG_LOCK:
            _REFRESH_RUNNING = False
        return False


def _read_cached(comp: dict) -> tuple:
    """READ-ONLY: return (observation, is_fresh). NEVER hits the network.
    Fresh cached entry → (obs, True); cold/stale → a 'pending' observation
    (all-unknown + refreshing note) → (obs, False)."""
    cached = _PROBE_CACHE.get(comp["slug"])
    if cached:
        try:
            age = (datetime.datetime.utcnow()
                   - cached["_cached_at"]).total_seconds()
        except Exception:
            age = _PROBE_TTL_SECONDS + 1
        if age < _PROBE_TTL_SECONDS:
            return _shape_observation(comp, cached["result"],
                                      cached["observed_at"]), True
    pending = _shape_observation(
        comp,
        {"axes": {k: "unknown" for k in
                  ("llms_txt", "mcp_server",
                   "machine_readable", "public_api_hint")},
         "axis_sources": {},
         "source_url": comp["homepage_url"],
         "note": ("Probe pending — observations refresh in the background "
                  "(read-only endpoint never blocks). Re-query shortly.")},
        _now_iso(),
    )
    return pending, False


def _comparison(force: bool = False) -> list[dict]:
    """Build the comparison array READ-ONLY from the probe cache — it NEVER
    blocks on the network in the request path (1-replica worker safety).
    Cold/stale entries come back as 'unknown (refreshing)' and we kick a
    single background warm so the next call is hot. `force` just schedules an
    immediate background refresh; it never blocks the request."""
    out: list[dict] = []
    any_cold = False
    for comp in _COMPETITORS:
        try:
            obs, fresh = _read_cached(comp)
        except Exception:
            obs, fresh = _shape_observation(
                comp,
                {"axes": {k: "unknown" for k in
                          ("llms_txt", "mcp_server",
                           "machine_readable", "public_api_hint")},
                 "axis_sources": {}, "source_url": comp["homepage_url"],
                 "note": "Not yet observed; axes unknown."},
                _now_iso()), False
        if not fresh:
            any_cold = True
        out.append(obs)
    if any_cold or force:
        _kick_background_refresh()   # non-blocking; warms for next call
    return out


def _dchub_block() -> dict:
    """The DC Hub differentiator block — stated true facts."""
    return {
        "name":     "DC Hub",
        "homepage": "https://dchub.cloud",
        "mcp":      "https://dchub.cloud/mcp",
        "differentiators": [
            {"key": d["key"], "label": d["label"], "value": d["value"],
             "proof": d["proof"]}
            for d in _DCHUB_DIFFERENTIATORS
        ],
    }


_METHODOLOGY = (
    "Competitor axes are observed from public surfaces on the date shown; "
    "absence is recorded as false only when a 200/404 was actually seen, "
    "else unknown. Pricing is never probed or asserted."
)


# ──────────────────────────────────────────────────────────────────────
# CORS / headers — mirrors agent_broadcast.py
# ──────────────────────────────────────────────────────────────────────

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Agent-Name, X-Admin-Key",
        "Cache-Control":                "public, max-age=300",
    }


def _admin_guard():
    """No-arg admin check → returns an error Response tuple, or None if OK.

    Mirrors routes/brain_v2_layer5.py::_admin_guard() and
    agent_broadcast.py::_admin_authorized(): accepts the key via the
    X-Admin-Key header or an ?admin_key= query param, validated against
    DCHUB_ADMIN_KEY (falling back to DCHUB_INTERNAL_KEY). If no admin key
    is configured in the environment, access is denied (fail-closed)."""
    import os
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    if not expected or provided != expected:
        return jsonify(
            {"ok": False, "error": "unauthorized",
             "hint": "X-Admin-Key header required"}), 401
    return None


# ──────────────────────────────────────────────────────────────────────
# 4. GET /api/v1/competitive/positioning  (PUBLIC, never 500)
# ──────────────────────────────────────────────────────────────────────

@competitive_intel_bp.route(
    "/api/v1/competitive/positioning", methods=["GET", "OPTIONS"]
)
def competitive_positioning():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    # Everything below is guarded so we NEVER 500. Worst case: dchub facts
    # plus comparison entries with all-"unknown" axes.
    try:
        force = (request.args.get("refresh") or "").lower() in (
            "1", "true", "yes")
        dchub = _dchub_block()
    except Exception:
        force, dchub = False, {"name": "DC Hub",
                               "homepage": "https://dchub.cloud",
                               "differentiators": []}
    try:
        comparison = _comparison(force=force)
    except Exception:
        comparison = []

    payload = {
        "ok":          True,
        "as_of":       _now_iso(),
        "dchub":       dchub,
        "comparison":  comparison,
        "methodology": _METHODOLOGY,
    }
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


# ──────────────────────────────────────────────────────────────────────
# 4b. POST/GET /api/v1/competitive/refresh  (ADMIN) — warm the probe cache
#     in the background. The 24x7 brain-ecosystem-watch cron hits this so
#     /positioning is virtually always hot. Returns IMMEDIATELY; probing
#     runs in a daemon thread and never blocks a worker.
# ──────────────────────────────────────────────────────────────────────

@competitive_intel_bp.route(
    "/api/v1/competitive/refresh", methods=["POST", "GET", "OPTIONS"]
)
def competitive_refresh():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    auth_err = _admin_guard()
    if auth_err:
        return auth_err
    started = _kick_background_refresh()
    resp = jsonify({
        "ok": True,
        "refresh_started": started,
        "note": ("Background probe warm "
                 + ("started" if started else "already in flight")
                 + "; re-query /api/v1/competitive/positioning shortly."),
    })
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


# ──────────────────────────────────────────────────────────────────────
# 5. GET /api/v1/competitive/why-dchub  (PUBLIC)
#    Positive, factual, agent-oriented — NO competitor names. Built for
#    embedding in llms.txt / agent-broadcast.
# ──────────────────────────────────────────────────────────────────────

@competitive_intel_bp.route(
    "/api/v1/competitive/why-dchub", methods=["GET", "OPTIONS"]
)
def why_dchub():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    try:
        bullets = [
            {"edge": d["label"], "detail": d["value"], "proof": d["proof"]}
            for d in _DCHUB_DIFFERENTIATORS
        ]
    except Exception:
        bullets = []

    pitch = (
        "DC Hub is the agent-native data-center intelligence platform. An "
        "AI agent can query it directly over a live MCP server (25+ tools), "
        "get real-time grid and energy data across 11 US ISOs / balancing "
        "authorities plus 3 international grids, read two proprietary daily "
        "indices (the DC Hub Power Index and the DC Hub Gas Index), and "
        "cite any answer via CC-BY-4.0 datasets with stable URLs and "
        "JSON-LD. It covers 21,000+ facilities and offers a free "
        "self-serve tier — so an agent can start in seconds, with no "
        "scraping, no PDFs, and no login wall."
    )
    payload = {
        "ok":             True,
        "as_of":          _now_iso(),
        "pitch":          pitch,
        "edges":          bullets,
        "citation_format": ("DC Hub (dchub.cloud), retrieved "
                            + datetime.date.today().isoformat()),
        "note":           ("Pure positive positioning — no competitor "
                           "claims. Safe to embed in llms.txt / "
                           "agent-broadcast."),
    }
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


# ──────────────────────────────────────────────────────────────────────
# 6. GET /api/v1/competitive/media-drafts  (ADMIN)
#    Returns 2-3 DRAFT educational post texts for the owner to review /
#    queue MANUALLY. Does NOT write to social_media_posts or any publish
#    queue. Drafts contain NO unverified competitor claims.
# ──────────────────────────────────────────────────────────────────────

@competitive_intel_bp.route(
    "/api/v1/competitive/media-drafts", methods=["GET", "OPTIONS"]
)
def media_drafts():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    guard = _admin_guard()
    if guard is not None:
        return guard

    drafts = [
        {
            "id":    "draft_agent_native",
            "title": "Why agent-native matters for data-center intel",
            "channel_hint": "linkedin",
            "text": (
                "Most data-center market intelligence was built for humans "
                "reading PDFs. DC Hub was built for the next reader: AI "
                "agents.\n\n"
                "We expose a live MCP server (25+ tools) so an agent can "
                "ask for facilities, markets, M&A, grid data, or our DCPI "
                "BUILD/AVOID verdicts and get structured JSON back — no "
                "scraping, no parsing, no login wall.\n\n"
                "If your team is wiring up AI workflows for site selection "
                "or diligence, this is the difference between an agent that "
                "can actually use your data and one that gives up at a "
                "paywall. Start free: dchub.cloud"
            ),
        },
        {
            "id":    "draft_cite_this",
            "title": "Citable, open, machine-readable",
            "channel_hint": "linkedin",
            "text": (
                "An AI answer is only as trustworthy as its sources. That's "
                "why DC Hub publishes core datasets and reports under "
                "CC-BY-4.0, with stable URLs and JSON-LD baked in.\n\n"
                "Our State of Data Center Power report scores ~285+ markets "
                "with live BUILD/AVOID verdicts (the DC Hub Power Index) "
                "and pairs it with the DC Hub Gas Index — and every figure "
                "links to a live endpoint an agent can cite.\n\n"
                "Open, dated, verifiable. dchub.cloud/state-of-power"
            ),
        },
        {
            "id":    "draft_live_grid",
            "title": "Real-time grid + energy, not last quarter's PDF",
            "channel_hint": "x",
            "text": (
                "Power is the constraint on every AI data-center build. So "
                "DC Hub tracks it live: real-time grid data across 11 US "
                "ISOs/balancing authorities + 3 international grids, fuel "
                "mix, interconnection-queue depth, and gas access by "
                "state.\n\n"
                "Ask an AI agent \"where can I get power in 90 days?\" and "
                "point it at our MCP server. Free tier, no card: dchub.cloud"
            ),
        },
    ]
    payload = {
        "ok":     True,
        "as_of":  _now_iso(),
        "drafts": drafts,
        "note":   ("DRAFTS ONLY — not published or queued. Review and "
                   "queue manually. Every claim is a verifiable DC Hub "
                   "fact; no competitor is named or characterised. Confirm "
                   "the live tool/market counts before posting if they may "
                   "have changed."),
    }
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200
