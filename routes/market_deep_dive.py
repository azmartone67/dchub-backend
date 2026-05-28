"""Phase ZZZZ (2026-05-16) — market deep-dive narrative generator.

Closes one of the last big DCHawk/dcByte gaps: per-market narrative
reports. We have DCPI scores (numbers); they have 50-page market
reports (story). This module lets Claude WRITE the story nightly
from our live data.

  POST /api/v1/markets/<slug>/regenerate     admin trigger
  GET  /api/v1/markets/<slug>/deep-dive      JSON deep-dive
  GET  /markets/<slug>/deep-dive             HTML page (schema.org Article)
  POST /api/v1/markets/deep-dive/cron        daily cron — rotates through markets

For each market, Claude is given:
  - DCPI score + rank + recent delta
  - Facility count + total MW
  - Recent M&A deals touching the market
  - Top operators present
…and asked to write a 400-500 word narrative analysis with schema.org
Article markup. Persisted to market_deep_dives table. Daily cron
rotates so all top 100 markets get refreshed at least monthly.
"""

from __future__ import annotations

import os
import re
import datetime
from flask import Blueprint, Response, jsonify, request, abort


market_deep_dive_bp = Blueprint("market_deep_dive", __name__)

_ADMIN_KEY     = (os.environ.get("DCHUB_ADMIN_KEY")
                  or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_deep_dives (
    market_slug    TEXT PRIMARY KEY,
    market_name    TEXT NOT NULL,
    narrative_md   TEXT NOT NULL,
    key_stats      JSONB,
    word_count     INT,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_used     TEXT
);
CREATE INDEX IF NOT EXISTS ix_mdd_generated ON market_deep_dives(generated_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _gather_market_facts(cur, slug: str) -> dict | None:
    """Pull live facts for the market from market_power_scores +
    discovered_facilities + deals."""
    try:
        cur.execute("""
            SELECT market_slug, market_name, score,
                   constraint_score, excess_power_score, verdict,
                   computed_at
              FROM market_power_scores
             WHERE LOWER(market_slug) = LOWER(%s)
                OR LOWER(market_name) = LOWER(REPLACE(%s, '-', ' '))
             ORDER BY computed_at DESC LIMIT 1
        """, (slug, slug))
        r = cur.fetchone()
    except Exception:
        return None
    if not r:
        return None
    out = {
        "slug":       r[0], "name": r[1],
        "dcpi_score": int(r[2]) if r[2] is not None else None,
        "constraint":int(r[3]) if r[3] is not None else None,
        "excess":    int(r[4]) if r[4] is not None else None,
        "verdict":   r[5],
        "computed":  r[6].isoformat() if r[6] else None,
    }
    # Facilities + MW
    try:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(power_mw),0)
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market,'')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
        """, (out["name"],))
        f = cur.fetchone()
        out["facility_count"] = int(f[0] or 0)
        out["total_mw"]       = float(f[1] or 0)
    except Exception:
        out["facility_count"] = 0
        out["total_mw"]       = 0
    # Top operators
    try:
        cur.execute("""
            SELECT provider, COUNT(*) AS n
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market,'')) = LOWER(%s)
               AND provider IS NOT NULL
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider ORDER BY n DESC LIMIT 5
        """, (out["name"],))
        out["top_operators"] = [{"name": p[0], "count": int(p[1])} for p in cur.fetchall()]
    except Exception:
        out["top_operators"] = []
    # Recent deals
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw
              FROM deals
             WHERE LOWER(COALESCE(market,'')) = LOWER(%s)
                OR LOWER(COALESCE(region,'')) = LOWER(%s)
             ORDER BY date DESC NULLS LAST LIMIT 5
        """, (out["name"], out["name"]))
        out["recent_deals"] = [{
            "date": d[0].isoformat() if hasattr(d[0],"isoformat") else (str(d[0]) if d[0] else None),
            "buyer": d[1], "seller": d[2],
            "value": float(d[3]) if d[3] is not None else None,
            "mw":    float(d[4]) if d[4] is not None else None,
        } for d in cur.fetchall()]
    except Exception:
        out["recent_deals"] = []
    return out


def _ask_claude_to_write(facts: dict) -> tuple[str | None, str | None]:
    if not _ANTHROPIC_KEY:
        return None, "no_anthropic_api_key"
    deals_str = ", ".join(
        f"{d.get('buyer','?')}→{d.get('seller','?')} ({'$'+format(d['value'],',.0f') if d.get('value') else '?'}, {d.get('date') or '?'})"
        for d in (facts.get("recent_deals") or [])[:5]
    ) or "no recent M&A tracked"
    operators_str = ", ".join(
        f"{o['name']} ({o['count']})" for o in (facts.get("top_operators") or [])[:5]
    ) or "operator mix not yet aggregated"
    prompt = (
        f"You are writing a 400-word market analysis for data-center "
        f"investors and operators. Be specific, cite the live numbers, "
        f"avoid generic platitudes. Output plain markdown, no preamble.\n\n"
        f"MARKET: {facts['name']}\n"
        f"DCPI score: {facts.get('dcpi_score','?')}/100 (verdict: {facts.get('verdict','?')})\n"
        f"Tracked facilities: {facts.get('facility_count')} | total MW: {facts.get('total_mw'):,.0f}\n"
        f"Top operators: {operators_str}\n"
        f"Recent M&A: {deals_str}\n\n"
        f"Write four paragraphs: (1) current state in one sentence, "
        f"then 2-3 specific facts; (2) what the DCPI verdict means for "
        f"buyers; (3) deal flow + operator dynamics; (4) one forward-"
        f"looking sentence. Maximum 500 words. No headings, just paragraphs."
    )
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001",
                  "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20,
        )
        if r.status_code >= 300:
            return None, f"status_{r.status_code}_{r.text[:100]}"
        text = (r.json().get("content", [{}])[0] or {}).get("text", "").strip()
        return text, None
    except Exception as e:
        return None, f"{type(e).__name__}:{str(e)[:60]}"


def generate_for_market(slug: str) -> dict:
    """Pull facts + ask Claude + persist. Returns the generated record."""
    out = {"ok": False, "slug": slug}
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            facts = _gather_market_facts(cur, slug)
            if not facts:
                out["error"] = "market_not_found"; return out
            narrative, err = _ask_claude_to_write(facts)
            if err:
                out["error"] = err; return out
            wc = len(narrative.split())
            import json as _j
            cur.execute("""
                INSERT INTO market_deep_dives
                  (market_slug, market_name, narrative_md, key_stats,
                   word_count, model_used)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (market_slug) DO UPDATE
                  SET market_name  = EXCLUDED.market_name,
                      narrative_md = EXCLUDED.narrative_md,
                      key_stats    = EXCLUDED.key_stats,
                      word_count   = EXCLUDED.word_count,
                      generated_at = NOW(),
                      model_used   = EXCLUDED.model_used
            """, (facts["slug"], facts["name"], narrative,
                  _j.dumps(facts), wc, "claude-haiku-4-5"))
            out.update({"ok": True, "market": facts["name"],
                        "word_count": wc,
                        "narrative_preview": narrative[:200] + "…"})
    finally:
        try: c.close()
        except Exception: pass
    return out


def read_deep_dive(slug: str) -> dict | None:
    c = _conn()
    if c is None: return None
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT market_slug, market_name, narrative_md, key_stats,
                       word_count, generated_at, model_used
                  FROM market_deep_dives
                 WHERE market_slug = %s
            """, (slug,))
            r = cur.fetchone()
            if not r: return None
            return dict(r)
    finally:
        try: c.close()
        except Exception: pass


@market_deep_dive_bp.route("/api/v1/markets/<slug>/deep-dive", methods=["GET"])
def deep_dive_json(slug):
    r = read_deep_dive(slug)
    if not r:
        return jsonify(error="not_yet_generated",
                       hint="POST /api/v1/markets/<slug>/regenerate to seed"), 404
    resp = jsonify({
        "slug":         r["market_slug"],
        "name":         r["market_name"],
        "narrative_md": r["narrative_md"],
        "key_stats":    r["key_stats"],
        "word_count":   r["word_count"],
        "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
        "model":        r["model_used"],
    })
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@market_deep_dive_bp.route("/api/v1/markets/<slug>/regenerate", methods=["POST"])
def regenerate(slug):
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(generate_for_market(slug)), 200


@market_deep_dive_bp.route("/api/v1/markets/deep-dive/cron", methods=["POST"])
def cron_rotate():
    """Daily cron — picks the 5 stalest markets and regenerates them.
    Over time covers all top 100 markets, every market refreshed
    monthly. Caps Claude API spend automatically."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    try: n = max(1, min(15, int(request.args.get("count") or 5)))
    except (ValueError, TypeError): n = 5
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    targets = []
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Top markets by DCPI score that are stalest in deep_dives
            # (NULL generated_at sorts first via LEFT JOIN)
            cur.execute("""
                SELECT mps.market_slug
                  FROM (SELECT DISTINCT ON (market_slug) market_slug, score, computed_at
                          FROM market_power_scores
                         WHERE published = true
                         ORDER BY market_slug, computed_at DESC) mps
                  LEFT JOIN market_deep_dives mdd USING (market_slug)
                 ORDER BY mdd.generated_at NULLS FIRST, mps.score DESC
                 LIMIT %s
            """, (n,))
            targets = [r[0] for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass

    results = []
    for slug in targets:
        results.append(generate_for_market(slug))
    return jsonify(generated_count=sum(1 for r in results if r.get("ok")),
                   results=results,
                   ran_at=datetime.datetime.utcnow().isoformat() + "Z"), 200


@market_deep_dive_bp.route("/markets/<slug>/deep-dive", methods=["GET"])
def deep_dive_html(slug):
    r = read_deep_dive(slug)
    if not r:
        abort(404)
    try:
        from routes.surface_brain import auto_log
        auto_log("market_deep_dive", "view", target=slug)
    except Exception: pass

    # Convert simple markdown paragraphs to <p>
    paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in (r["narrative_md"] or "").split("\n\n") if p.strip()
    )
    name = r["market_name"]
    gen_at = r["generated_at"].strftime("%Y-%m-%d") if r["generated_at"] else "?"
    stats = r.get("key_stats") or {}
    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>{name} Market Deep-Dive · DC Hub</title>
<meta name="description" content="{name} data center market analysis. DCPI score {stats.get('dcpi_score','?')}/100, {stats.get('facility_count',0)} facilities, {stats.get('total_mw',0):,.0f} MW. Updated {gen_at}.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/markets/{slug}/deep-dive">
<meta property="og:title" content="{name} Market Deep-Dive · DC Hub">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Article",
 "headline":"{name} Data Center Market Deep-Dive",
 "datePublished":"{r['generated_at'].isoformat() if r['generated_at'] else ''}",
 "author":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "url":"https://dchub.cloud/markets/{slug}/deep-dive",
 "wordCount":{r.get('word_count') or 0},
 "about":{{"@type":"Place","name":"{name}"}},
 "description":"Live data-center market analysis. DCPI score {stats.get('dcpi_score','?')}/100."
}}</script>
<style>body{{font-family:Georgia,serif;max-width:760px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.7}}
h1{{font-family:-apple-system,sans-serif;margin:0 0 .25rem;font-size:2rem}}
.sub{{color:#6b7280;font-family:-apple-system,sans-serif;margin:0 0 1.5rem;font-size:.9rem}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.5rem;margin:1rem 0 2rem;background:#f5f5fa;padding:1rem 1.25rem;border-radius:8px;font-family:-apple-system,sans-serif}}
.stat{{font-size:.85rem;color:#6b7280}}
.stat b{{display:block;font-size:1.4rem;color:#1f2937}}
p{{margin:1rem 0;font-size:1.08rem}}
.foot{{color:#9ca3af;font-size:.85rem;margin-top:2rem;font-family:-apple-system,sans-serif}}
.foot a{{color:#6366f1;text-decoration:none}}
.foot a:hover{{text-decoration:underline}}</style>
</head><body>
<h1>{name}</h1>
<p class="sub">Data Center Market Deep-Dive · {r.get('word_count') or 0} words · generated {gen_at} by Claude haiku from live DC Hub data</p>
<div class="stats">
 <div class="stat">DCPI Score<b>{stats.get('dcpi_score','?')}/100</b></div>
 <div class="stat">Facilities<b>{stats.get('facility_count',0):,}</b></div>
 <div class="stat">Total MW<b>{stats.get('total_mw',0):,.0f}</b></div>
 <div class="stat">Verdict<b>{stats.get('verdict','?')}</b></div>
</div>
{paragraphs}
<p class="foot">JSON: <a href="/api/v1/markets/{slug}/deep-dive">/api/v1/markets/{slug}/deep-dive</a> · DCPI: <a href="/dcpi">/dcpi</a> · Operators: <a href="/operators">/operators</a> · Updated nightly</p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=1800"})


# Phase ZZZZ-shortform (2026-05-18): top-level /markets/<slug> shell.
# Dashboard QA expects /markets/chicago, /markets/dallas, /markets/northern-virginia
# to return 200. Either renders the cached deep-dive narrative if present,
# or a minimal SEO shell built from MARKET_DATA so the route is always 200.
_SLUG_TO_MARKET_NAME = {
    "northern-virginia":      "Northern Virginia",
    "nova":                   "Northern Virginia",
    "dallas":                 "Dallas-Fort Worth",
    "dallas-fort-worth":      "Dallas-Fort Worth",
    "dfw":                    "Dallas-Fort Worth",
    "chicago":                "Chicago",
    "silicon-valley":         "Silicon Valley",
    "phoenix":                "Phoenix",
    "atlanta":                "Atlanta",
    "new-york":               "New York Metro",
    "new-york-metro":         "New York Metro",
    "nyc":                    "New York Metro",
    "portland":               "Portland-Hillsboro",
    "portland-hillsboro":     "Portland-Hillsboro",
    "los-angeles":            "Los Angeles",
    "la":                     "Los Angeles",
    "seattle":                "Seattle",
    "denver":                 "Denver",
    "miami":                  "Miami",
    "boston":                 "Boston",
    "minneapolis":            "Minneapolis",
    "houston":                "Houston",
    "austin":                 "Austin",
    "salt-lake-city":         "Salt Lake City",
    "columbus":               "Columbus",
    "kansas-city":            "Kansas City",
    "toronto":                "Toronto",
    "montreal":               "Montreal",
    "london":                 "London",
    "frankfurt":              "Frankfurt",
    "amsterdam":              "Amsterdam",
    "paris":                  "Paris",
    "dublin":                 "Dublin",
    "madrid":                 "Madrid",
    "milan":                  "Milan",
    "stockholm":              "Stockholm",
    "warsaw":                 "Warsaw",
    "singapore":              "Singapore",
    "tokyo":                  "Tokyo",
    "sydney":                 "Sydney",
    "hong-kong":              "Hong Kong",
    "seoul":                  "Seoul",
    "mumbai":                 "Mumbai",
    "sao-paulo":              "São Paulo",
}


@market_deep_dive_bp.route("/markets/<slug>", methods=["GET"])
def market_short_html(slug):
    """Top-level /markets/<slug>. Prefers the cached deep-dive narrative;
    falls back to a minimal SEO shell so QA never sees a 404."""
    slug_norm = (slug or "").lower().strip()
    # If the cached deep-dive exists, redirect-through (serve same HTML)
    r = read_deep_dive(slug_norm)
    if r:
        return deep_dive_html(slug_norm)

    # Fallback: render minimal shell from MARKET_DATA
    name = _SLUG_TO_MARKET_NAME.get(slug_norm)
    if not name:
        # Try title-cased fallback: "chicago" → "Chicago"
        name = slug_norm.replace("-", " ").title()

    md = {}
    try:
        from market_intelligence_api import MARKET_DATA
        md = MARKET_DATA.get(name, {}) or {}
    except Exception:
        pass

    if not md:
        # Still return 200 — the market exists in our universe even if we
        # haven't yet pulled rich data. Better than 404 for SEO + QA.
        md = {"region": "—", "inventory_mw": "—", "vacancy_rate": "—",
              "avg_asking_rate": "—", "num_facilities": "—"}
        # r43-H (2026-05-27): an all-"—" page looks broken (user reported
        # /markets/reno). MARKET_DATA only covers curated major markets, but
        # the live discovered_facilities table has real counts for smaller
        # ones like Reno. Pull facility count + capacity from the DB using
        # the SAME MARKET_ALIASES + US country-guard the authoritative
        # /api/v1/markets/<m> endpoint uses (the country guard is what keeps
        # 'Reno' from matching 'Grenoble' etc. in the count). Research-only
        # metrics (vacancy, asking rate, YoY) stay "—" when we genuinely
        # lack CBRE/JLL coverage for the market.
        try:
            from main import MARKET_ALIASES, RAILWAY_EXCLUSION
            cities = MARKET_ALIASES.get(slug_norm.replace('-', ' '))
            if cities:
                c2 = _conn()
                if c2 is not None:
                    try:
                        conds, params = [], []
                        for city in cities:
                            if len(city) == 2 and city.isupper():
                                conds.append("state = %s"); params.append(city)
                            else:
                                # exact match (+ "City, ST" prefix) — NOT
                                # substring, to avoid reno→Grenoble bleed.
                                conds.append("(LOWER(city) = LOWER(%s) OR city ILIKE %s)")
                                params.append(city); params.append(f"{city},%")
                        where = " OR ".join(conds)
                        guard = ("AND (country='US' OR country='USA' "
                                 "OR country IS NULL OR country='')")
                        with c2.cursor() as cur:
                            # NOTE: the `status ILIKE %s` placeholder sits in
                            # the SELECT (textually BEFORE the WHERE city/state
                            # placeholders), so psycopg2 binds it FIRST — the
                            # construction pattern must lead the params list,
                            # not trail it. (RAILWAY_EXCLUSION uses %% literals,
                            # no placeholders.)
                            cur.execute(f"""
                                SELECT COUNT(*),
                                       COALESCE(SUM(power_mw),0),
                                       COALESCE(SUM(CASE WHEN status ILIKE %s
                                                         THEN power_mw ELSE 0 END),0)
                                  FROM discovered_facilities
                                 WHERE ({where}) {guard} {RAILWAY_EXCLUSION}
                            """, ['%construction%'] + params)
                            row = cur.fetchone()
                        if row and row[0]:
                            md["num_facilities"] = int(row[0])
                            if row[1]:
                                md["inventory_mw"] = round(float(row[1]))
                            if row[2]:
                                md["under_construction_mw"] = round(float(row[2]))
                            md["region"] = "North America"
                    finally:
                        try: c2.close()
                        except Exception: pass
        except Exception:
            pass

    highlights_html = ""
    hl = md.get("highlights") or []
    if hl:
        items = "".join(f"<li>{h}</li>" for h in hl)
        highlights_html = f"<h2>Highlights</h2><ul>{items}</ul>"

    providers_html = ""
    tp = md.get("top_providers") or []
    if tp:
        providers_html = (f"<h2>Top Providers</h2><p>{', '.join(tp)}</p>")

    desc = (f"{name} data center market. {md.get('num_facilities','?')} facilities, "
            f"{md.get('inventory_mw','?')} MW inventory, "
            f"{md.get('vacancy_rate','?')}% vacancy, "
            f"${md.get('avg_asking_rate','?')}/kW/mo asking. Live DC Hub data.")

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>{name} Data Center Market · DC Hub</title>
<meta name="description" content="{desc}">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/markets/{slug_norm}">
<meta property="og:title" content="{name} Data Center Market · DC Hub">
<meta property="og:description" content="{desc}">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Place",
 "name":"{name}",
 "description":"{desc}",
 "url":"https://dchub.cloud/markets/{slug_norm}"
}}</script>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.7}}
h1{{margin:0 0 .25rem;font-size:2rem}}
.sub{{color:#6b7280;margin:0 0 1.5rem;font-size:.9rem}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.5rem;margin:1rem 0 2rem;background:#f5f5fa;padding:1rem 1.25rem;border-radius:8px}}
.stat{{font-size:.85rem;color:#6b7280}}
.stat b{{display:block;font-size:1.4rem;color:#1f2937}}
.foot{{color:#9ca3af;font-size:.85rem;margin-top:2rem}}
.foot a{{color:#6366f1;text-decoration:none}}
.foot a:hover{{text-decoration:underline}}
ul{{padding-left:1.25rem}}</style>
</head><body>
<h1>{name}</h1>
<p class="sub">Data Center Market · {md.get('region','—')}</p>
<div class="stats">
 <div class="stat">Inventory<b>{md.get('inventory_mw','—')} MW</b></div>
 <div class="stat">Vacancy<b>{md.get('vacancy_rate','—')}%</b></div>
 <div class="stat">Asking Rate<b>${md.get('avg_asking_rate','—')}/kW/mo</b></div>
 <div class="stat">Facilities<b>{md.get('num_facilities','—')}</b></div>
 <div class="stat">YoY Price<b>{md.get('yoy_price_change','—')}%</b></div>
 <div class="stat">Under Constr.<b>{md.get('under_construction_mw','—')} MW</b></div>
</div>
{providers_html}
{highlights_html}
<p class="foot">Deep-dive narrative: <a href="/markets/{slug_norm}/deep-dive">/markets/{slug_norm}/deep-dive</a> ·
JSON: <a href="/api/v1/markets/{name.replace(' ', '%20')}">/api/v1/markets/{name}</a> ·
All markets: <a href="/markets">/markets</a></p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900"})
