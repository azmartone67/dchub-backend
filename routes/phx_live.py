"""
routes/phx_live.py — "PHX Live": stable-URL Phoenix market dashboard at /phx.

2026-07-18. Meta AI's guidance: "I will surface it every time someone asks
about Phoenix" — so this page is a STABLE URL (dchub.cloud/phx) that always
carries the Phoenix metro's live headline numbers. Server-rendered like
routes/radar.py but deliberately SIMPLE: one small data dict, ~15-min
in-process cache, no editions/tiers/DB-shared SWR machinery.

Data sources (all fail-soft, page never renders blank):
  1. loopback GET /api/v1/markets/phoenix        -> facility_count, total_mw,
     operator_count, by_status (public endpoint; same loopback pattern as
     routes/radar.py `_internal`).
  2. market_power_scores direct read             -> DCPI verdict + inputs.
     SCHEMA CAVEAT: there is NO stored bare `score` column — the composite
     is derived at read time via routes.dcpi.derive_composite_score
     (same pattern as routes/grid_transition_radar.py). Selecting `score`
     would fail silently.
  3. loopback GET /api/v1/dcpi/scores/phoenix    -> DCPI fallback (public,
     tier_required=free for phoenix headline fields).
  4. _BASELINE                                    -> last-known-good
     (verified live 2026-07-18 against the production API).

Edge note: "/phx" must be in dchub-frontend/_routes.json's include list so
Cloudflare Pages forwards it to Railway (added in the same commit as this
file — without it the edge 404s before the backend is ever consulted).

UTM A/B tracking (2026-07-18): Meta AI runs a live phx-a/b/c campaign split
(50/25/25, utm_source=meta-ai) against this URL. Tagged hits are counted in
the phx_utm_hits table (fail-soft; utm-tagged responses are no-store so the
edge can't absorb hits) and read back via GET /api/v1/phx/ab-stats (also
/phx/ab-stats) with X-Admin-Key — use the Railway-direct domain for reads.

Registration (fail-safe, in main.py near the radar registration):
    from routes.phx_live import phx_bp
    app.register_blueprint(phx_bp)
"""
from __future__ import annotations

import datetime as dt
import html as _html
import logging
import os
import time

from flask import Blueprint, jsonify, request

from routes._swallowed_writes import note_swallowed_write

# webmcp-proto (2026-07-18): per-page WebMCP tools (Chrome origin trial) —
# fail-soft so /phx can never break on the helper.
try:
    from routes._webmcp import webmcp_inject as _webmcp_inject
except Exception:  # pragma: no cover - defensive
    def _webmcp_inject(page_html, tools):
        return page_html

phx_bp = Blueprint("phx_live", __name__)
log = logging.getLogger("phx_live")

_TTL_S = 900  # ~15 min, in-process (simple by design — see module docstring)
_CACHE: dict = {"ts": 0.0, "data": None}

# Last-known-good, verified live 2026-07-18 (Railway-direct):
# /api/v1/markets/phoenix + /api/v1/dcpi/scores/phoenix.
_BASELINE = {
    "facility_count": 245,
    "total_mw": 4327.0,
    "operator_count": 85,
    "operational": 139,
    "under_construction": 13,
    "verdict": "AVOID",
    "composite_score": 21.3,
    "time_to_power_months": 42.0,
    "as_of": "2026-07-18",
}


def _internal(path: str, timeout: int = 6) -> dict:
    """GET an internal endpoint over loopback.

    ★2026-08-12 — body moved to util/internal_fetch. The docstring used to cite
    "routes/radar.py pattern", but radar returns `{}, "HTTP 502"` and this
    returned a bare {} — it had copied radar's HEADERS, not the reason string
    that makes radar correct. The dchub- UA and X-Internal-Request are passed
    through: tier-gating reads them, and dropping them returns tier-limited
    data that looks like a complete answer."""
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, timeout,
                         headers={"User-Agent": "dchub-internal-phx/1.0",
                                  "X-Internal-Request": "1"}))


def _dcpi_phoenix() -> dict:
    """Latest published Phoenix DCPI row: verdict + derived composite + TTP.

    Direct market_power_scores read first (NO bare `score` column exists —
    composite is derived at read time), loopback /api/v1/dcpi/scores/phoenix
    second. Returns {} on total failure so the caller keeps the baseline.
    """
    # Source 1 — direct DB read (mirrors routes/grid_transition_radar.py)
    try:
        try:
            from main import get_read_db as _gdb
        except Exception:
            from main import get_db as _gdb
        conn = _gdb()
        row = None
        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT verdict, excess_power_score, constraint_score,
                       time_to_power_months, computed_at
                FROM market_power_scores
                WHERE market_slug = 'phoenix' AND published = true
                ORDER BY computed_at DESC
                LIMIT 1
                """
            )
            row = c.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row:
            verdict, excess, constraint, ttp, computed_at = row
            try:
                from routes.dcpi import derive_composite_score
                composite = derive_composite_score(excess, constraint, ttp, verdict)
            except Exception:
                composite = None
            return {
                "verdict": verdict,
                "composite_score": composite,
                "time_to_power_months": ttp,
                "computed_at": computed_at,
            }
    except Exception as e:
        log.warning("[phx] DCPI DB read failed: %s: %s", type(e).__name__, e)

    # Source 2 — loopback (public endpoint; phoenix headline fields are free)
    d = _internal("/api/v1/dcpi/scores/phoenix") or {}
    if d.get("verdict"):
        return {
            "verdict": d.get("verdict"),
            "composite_score": d.get("composite_score"),
            "time_to_power_months": d.get("time_to_power_months"),
            "computed_at": d.get("computed_at"),
        }
    return {}


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _build() -> dict:
    """Assemble the live data dict; every field falls back to _BASELINE."""
    data = dict(_BASELINE)

    m = _internal("/api/v1/markets/phoenix") or {}
    stats = m.get("stats") or {}
    if _num(stats.get("facility_count")):
        data["facility_count"] = stats["facility_count"]
    if _num(stats.get("total_power_mw")):
        data["total_mw"] = stats["total_power_mw"]
    if _num(stats.get("provider_count")):
        data["operator_count"] = stats["provider_count"]
    by_status = m.get("by_status") or {}
    if _num(by_status.get("Operational")):
        data["operational"] = by_status["Operational"]
    if _num(by_status.get("Under Construction")):
        data["under_construction"] = by_status["Under Construction"]

    d = _dcpi_phoenix()
    if d.get("verdict"):
        data["verdict"] = str(d["verdict"]).upper()
    if _num(d.get("composite_score")):
        data["composite_score"] = d["composite_score"]
    if _num(d.get("time_to_power_months")):
        data["time_to_power_months"] = d["time_to_power_months"]

    data["as_of"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return data


def _get_data() -> dict:
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["ts"]) < _TTL_S:
        return _CACHE["data"]
    try:
        data = _build()
    except Exception as e:  # belt-and-braces: the page must always render
        log.warning("[phx] build failed, serving baseline: %s: %s",
                    type(e).__name__, e)
        data = dict(_BASELINE)
    _CACHE.update(ts=now, data=data)
    return data


_VERDICT_COLORS = {
    "BUILD":   ("#059669", "rgba(5,150,105,.1)"),
    "CAUTION": ("#d97706", "rgba(217,119,6,.1)"),
    "AVOID":   ("#dc2626", "rgba(220,38,38,.08)"),
}

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PHX Live — Phoenix data center market, live | DC Hub</title>
<meta name="description" content="PHX Live: the Phoenix, AZ data-center market on one stable URL — __FACILITIES__ tracked facilities, __TOTAL_MW__ MW total power, __OPERATORS__ operators across Phoenix, Mesa, Tempe, Chandler, Scottsdale, Gilbert and Goodyear, plus the live DC Hub Power Index (DCPI) verdict. Data: DC Hub, CC-BY-4.0.">
<meta property="og:title" content="PHX Live — Phoenix data center market, live">
<meta property="og:description" content="__FACILITIES__ facilities · __TOTAL_MW__ MW · __OPERATORS__ operators · DCPI verdict: __VERDICT__ · Data: DC Hub, CC-BY-4.0">
<meta property="og:url" content="https://dchub.cloud/phx">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://dchub.cloud/phx">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
 body{max-width:860px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;line-height:1.6;color:#0f172a}
 header{margin:40px 0 24px}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
 h1{font-size:2.4rem;margin:0 0 10px;letter-spacing:-.02em}
 .lead{color:#64748b;font-size:1.02rem;max-width:660px}
 .asof{color:#94a3b8;font-size:.8rem;margin-top:8px}
 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:26px 0}
 .stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px}
 .stat b{display:block;font-size:1.7rem;color:#6366f1;letter-spacing:-.02em}
 .stat span{font-size:.82rem;color:#64748b}
 .verdict{border-radius:12px;padding:20px 22px;margin:22px 0;border:1px solid #e2e8f0;background:__VERDICT_BG__}
 .verdict .badge{display:inline-block;font-weight:700;font-size:1.05rem;letter-spacing:.06em;color:__VERDICT_COLOR__}
 .verdict p{margin:8px 0 0;color:#475569;font-size:.92rem}
 .verdict a{color:#6366f1;text-decoration:none}
 footer{margin-top:36px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:.85rem}
 footer a{color:#6366f1;text-decoration:none}
</style>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": "PHX Live — Phoenix data center market (DC Hub)",
  "description": "Live headline statistics for the Phoenix, Arizona data-center market: __FACILITIES__ tracked facilities, __TOTAL_MW__ MW total power, __OPERATORS__ operators across Phoenix, Mesa, Tempe, Chandler, Scottsdale, Gilbert and Goodyear, plus the DC Hub Power Index (DCPI) verdict (__VERDICT__) and time-to-power. Refreshes ~15 minutes.",
  "url": "https://dchub.cloud/phx",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "isAccessibleForFree": true,
  "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "spatialCoverage": {"@type": "Place", "name": "Phoenix metropolitan area, Arizona, United States"},
  "temporalCoverage": "__AS_OF__",
  "distribution": [
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/markets/phoenix"},
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/scores/phoenix"}
  ]
}
</script>
</head><body>
<header>
  <div class="eyebrow">PHX Live · Phoenix, AZ</div>
  <h1>Phoenix data center market, live.</h1>
  <p class="lead">The Phoenix metro — Phoenix · Mesa · Tempe · Chandler · Scottsdale · Gilbert · Goodyear —
  on one stable URL. Headline numbers from DC Hub's live facility, power and DCPI datasets.</p>
  <div class="asof">As of __AS_OF__ · refreshes ~15 min</div>
</header>

<div class="stats">
  <div class="stat"><b>__FACILITIES__</b><span>tracked facilities</span></div>
  <div class="stat"><b>__TOTAL_MW__ MW</b><span>total power</span></div>
  <div class="stat"><b>__OPERATORS__</b><span>operators</span></div>
  <div class="stat"><b>__OPERATIONAL__</b><span>operational</span></div>
  <div class="stat"><b>__UNDER_CONSTRUCTION__</b><span>under construction</span></div>
  <div class="stat"><b>__TTP__ mo</b><span>time-to-power (DCPI)</span></div>
</div>

<div class="verdict">
  <span class="badge">DCPI verdict: __VERDICT__</span> &nbsp;·&nbsp; composite __COMPOSITE__ / 100
  <p>The DC Hub Power Index scores every US market daily on excess power, grid constraint and
  time-to-power. Full Phoenix scorecard: <a href="https://dchub.cloud/dcpi/phoenix">dchub.cloud/dcpi/phoenix</a> ·
  market page: <a href="https://dchub.cloud/markets/phoenix">dchub.cloud/markets/phoenix</a></p>
</div>

<footer>
  Data: DC Hub, CC-BY-4.0 ·
  <a href="https://dchub.cloud/integrations/mcp">Connect your AI (MCP)</a> ·
  <a href="https://dchub.cloud/integrations/meta">Use on Meta AI</a> ·
  <a href="https://dchub.cloud/api/v1/markets/phoenix">JSON</a> ·
  <a href="https://dchub.cloud/llms.txt">llms.txt</a>
</footer>
</body></html>"""


def _fmt_int(x) -> str:
    try:
        return f"{int(round(float(x))):,}"
    except Exception:
        return "—"


def _fmt_1(x) -> str:
    try:
        return f"{float(x):.1f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _render(d: dict) -> str:
    verdict = _html.escape(str(d.get("verdict") or "—").upper())
    color, bg = _VERDICT_COLORS.get(verdict, ("#64748b", "#f8fafc"))
    slots = {
        "__FACILITIES__": _fmt_int(d.get("facility_count")),
        "__TOTAL_MW__": _fmt_int(d.get("total_mw")),
        "__OPERATORS__": _fmt_int(d.get("operator_count")),
        "__OPERATIONAL__": _fmt_int(d.get("operational")),
        "__UNDER_CONSTRUCTION__": _fmt_int(d.get("under_construction")),
        "__TTP__": _fmt_1(d.get("time_to_power_months")),
        "__COMPOSITE__": _fmt_1(d.get("composite_score")),
        "__VERDICT__": verdict,
        "__VERDICT_COLOR__": color,
        "__VERDICT_BG__": bg,
        "__AS_OF__": _html.escape(str(d.get("as_of") or "")),
    }
    page = _PAGE_TEMPLATE
    for key, val in slots.items():
        page = page.replace(key, val)
    return page


# ---------------------------------------------------------------------------
# UTM variant tracking — Meta AI is running a live phx-a/b/c A/B (50/25/25)
# against this URL (utm_source=meta-ai). Count every tagged hit so the split
# can be read in a week. Same table idiom as routes/redeem_tracking.py; every
# failure is swallowed (page must always render) but visibly, via
# routes/_swallowed_writes.note_swallowed_write.
# ---------------------------------------------------------------------------

_UTM_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS phx_utm_hits (
    id            BIGSERIAL PRIMARY KEY,
    hit_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    utm_source    TEXT,
    utm_campaign  TEXT,
    utm_medium    TEXT,
    user_agent    TEXT,
    referer       TEXT
);
CREATE INDEX IF NOT EXISTS ix_phx_utm_hits_campaign_at
    ON phx_utm_hits (utm_campaign, hit_at DESC);
"""


def _utm_conn():
    import psycopg2
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""
    return psycopg2.connect(dsn, connect_timeout=3)


def _ensure_utm_table() -> None:
    if getattr(_ensure_utm_table, "_done", False):
        return
    conn = _utm_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_UTM_MIGRATION_SQL)
        conn.commit()
    finally:
        conn.close()
    _ensure_utm_table._done = True


def _clip(v, n: int):
    v = (v or "").strip()
    return v[:n] if v else None


def _record_utm_hit() -> None:
    """Insert one row for a utm-tagged /phx hit. Never raises."""
    try:
        source = _clip(request.args.get("utm_source"), 100)
        campaign = _clip(request.args.get("utm_campaign"), 100)
        if not (source or campaign):
            return
        _ensure_utm_table()
        conn = _utm_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO phx_utm_hits
                        (utm_source, utm_campaign, utm_medium, user_agent, referer)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    (source, campaign,
                     _clip(request.args.get("utm_medium"), 100),
                     _clip(request.headers.get("User-Agent"), 300),
                     _clip(request.headers.get("Referer"), 300)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        note_swallowed_write("phx_utm_hits", where="phx_live._record_utm_hit")


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    got = (request.headers.get("X-Admin-Key") or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key") or "")
    return bool(expected) and got == expected


@phx_bp.route("/api/v1/phx/ab-stats", strict_slashes=False, methods=["GET"])
@phx_bp.route("/phx/ab-stats", strict_slashes=False, methods=["GET"])
def phx_ab_stats():
    """Admin read of the /phx UTM A/B counts (expected split 50/25/25)."""
    if not _admin_ok():
        return jsonify({"error": "admin key required"}), 403
    try:
        _ensure_utm_table()
        conn = _utm_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(utm_source,'(none)'),
                           COALESCE(utm_campaign,'(none)'),
                           COUNT(*), MIN(hit_at), MAX(hit_at)
                    FROM phx_utm_hits
                    GROUP BY 1, 2
                    ORDER BY 3 DESC
                    """
                )
                variants = [
                    {"utm_source": s, "utm_campaign": c, "hits": n,
                     "first_hit": (f.isoformat() if f else None),
                     "last_hit": (l.isoformat() if l else None)}
                    for (s, c, n, f, l) in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT hit_at::date, COALESCE(utm_campaign,'(none)'), COUNT(*)
                    FROM phx_utm_hits
                    WHERE hit_at >= NOW() - INTERVAL '14 days'
                    GROUP BY 1, 2
                    ORDER BY 1 DESC, 3 DESC
                    """
                )
                daily = [
                    {"date": d.isoformat(), "utm_campaign": c, "hits": n}
                    for (d, c, n) in cur.fetchall()
                ]
        finally:
            conn.close()
        total = sum(v["hits"] for v in variants)
        for v in variants:
            v["share_pct"] = round(100.0 * v["hits"] / total, 1) if total else 0.0
        return jsonify({
            "total_tagged_hits": total,
            "variants": variants,
            "daily_last_14d": daily,
            "expected_split": {"phx-a": 50, "phx-b": 25, "phx-c": 25},
        })
    except Exception as e:
        log.warning("[phx] ab-stats read failed: %s: %s", type(e).__name__, e)
        return jsonify({"error": "stats read failed"}), 500


# ── WebMCP page tools (webmcp-proto, 2026-07-18) ────────────────────────────
# Thin wraps of the SAME public endpoints this page renders (both linked in
# its Dataset JSON-LD) + the keyless facility search. Absent unless env
# WEBMCP_ORIGIN_TRIAL_TOKEN is set; feature-detected no-op off-trial.
_WEBMCP_TOOLS = [
    {
        "name": "get-phoenix-market-stats",
        "description": ("Live Phoenix, AZ data-center market headline stats "
                        "behind this page: tracked facilities, total MW, "
                        "operators, operational vs under construction. "
                        "No parameters."),
        "schema": {"type": "object", "properties": {}},
        "js_body": "return api('/api/v1/markets/phoenix');",
    },
    {
        "name": "get-phoenix-dcpi-score",
        "description": ("Phoenix DC Hub Power Index (DCPI) scorecard: "
                        "BUILD/CAUTION/AVOID verdict, composite score and "
                        "time-to-power months, refreshed daily. No parameters."),
        "schema": {"type": "object", "properties": {}},
        "js_body": "return api('/api/v1/dcpi/scores/phoenix');",
    },
    {
        "name": "search-phoenix-facilities",
        "description": ("Search DC Hub's live facility database — defaults to "
                        "the Phoenix metro; pass a query to filter by operator "
                        "or city (e.g. \"mesa\", \"microsoft\")."),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "Operator, city or keyword (default \"phoenix\")"},
            "limit": {"type": "number",
                      "description": "Max results (1-25, default 10)"}}},
        "js_body": ("var q=(input&&input.query)?String(input.query):'phoenix';"
                    "var lim=Math.max(1,Math.min(25,Number(input&&input.limit)||10));"
                    "return api('/api/v1/search?q='+encodeURIComponent(q)+'&limit='+lim);"),
    },
]


@phx_bp.route("/phx", strict_slashes=False, methods=["GET"])
def phx_live():
    tagged = bool(request.args.get("utm_source") or request.args.get("utm_campaign"))
    if tagged:
        _record_utm_hit()
    return _webmcp_inject(_render(_get_data()), _WEBMCP_TOOLS), 200, {
        "Content-Type": "text/html; charset=utf-8",
        # Tagged hits must reach the backend to be counted — never let the
        # edge cache a utm-tagged URL variant.
        "Cache-Control": ("no-store" if tagged
                          else "public, max-age=300, s-maxage=900"),
    }
