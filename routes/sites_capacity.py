"""
sites_capacity.py — Phase GG: per-site Capacity Report.

The user's ask: "a capacity site report that tracks data centers, and
available capacity per site in an easy to access self learning and
self aware site."

One endpoint, one bundled response, pure DB reads. Mirrors the
get_market_brief shape (PR #138) but at the per-facility level instead
of per-market. An MCP agent — or the /sites/<slug> frontend — can pull
the whole picture in a single call.

  GET /api/v1/sites/<id_or_slug>/capacity-report

Bundled payload:
    site              {id, name, provider, city, state, country, lat, lon,
                       status, power_mw, source}
    capacity          {operational_mw, under_construction_mw, planned_mw,
                       total_mw, utilization_pct}
    pipeline          [list of capacity_pipeline rows tied to this site/market]
    grid              {iso, verdict (from DCPI), constraint, excess_power,
                       time_to_power_months}  — if the site's market is DCPI-scored
    market            {slug, name, peer_count, top_peers[]}
    news              [most-recent news rows mentioning the site]
    drill_deeper      pointers to MCP tools for depth

Self-awareness: this report registers `sites_capacity` as a freshness-
radar domain so the radar surfaces stale facility ingestion.
"""
import json
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

sites_capacity_bp = Blueprint("sites_capacity", __name__)


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True   # read-only module — keep each query independent so a
    return c              # type/missing-column error can't poison the rest.


def _norm_slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_site(cur, ident):
    """Resolve a site by numeric id, hashed slug (provider-name-<8hex>, the
    /api/v1/facility URL format), or fuzzy name. Searches BOTH
    discovered_facilities and facilities.

    r43-H (2026-05-28): the old version did `WHERE id = int(ident)` against
    facilities, whose id is TEXT → 'operator does not exist: text = integer',
    which aborted the (non-autocommit) transaction and broke the whole report.
    It also only searched `facilities` for slugs/names and had no handler for
    the hashed-slug format, so /sites/<slug> always 404'd. Now: compare with
    `id::text` (never type-errors), add the LEFT(MD5(id::text),8) slug match
    that /api/v1/facility uses, and fall back to a reduced column set if a
    table lacks source/first_seen."""
    cols = ("id, name, provider, city, state, country, latitude, longitude, "
            "status, power_mw, source, first_seen")
    safe_cols = ("id, name, provider, city, state, country, latitude, longitude, "
                 "status, power_mw, NULL AS source, NULL AS first_seen")
    tables = ("discovered_facilities", "facilities")
    s = str(ident).strip()

    def _q(tbl, where, params):
        for cset in (cols, safe_cols):
            try:
                cur.execute(f"SELECT {cset} FROM {tbl} WHERE {where} LIMIT 1", params)
                return cur.fetchone()
            except Exception:
                try: cur.connection.rollback()
                except Exception: pass
        return None

    # 1) exact id — compare as text so a TEXT or INT id column both work
    if s.isdigit():
        for tbl in tables:
            row = _q(tbl, "id::text = %s", (s,))
            if row:
                return row, tbl

    # 2) hashed slug — /api/v1/facility builds slugs ending in LEFT(MD5(id::text),8)
    tail = s.rsplit("-", 1)[-1].lower()
    if len(tail) == 8 and all(ch in "0123456789abcdef" for ch in tail):
        for tbl in tables:
            row = _q(tbl, "LEFT(MD5(id::text), 8) = %s", (tail,))
            if row:
                return row, tbl

    # 3) fuzzy name match — strip hyphens and any trailing 8-hex id token
    name_q = s.replace("-", " ").replace("_", " ").strip()
    parts = name_q.split()
    if parts and len(parts[-1]) == 8 and all(ch in "0123456789abcdef" for ch in parts[-1].lower()):
        name_q = " ".join(parts[:-1]).strip()
    if name_q:
        for tbl in tables:
            row = _q(tbl, "LOWER(name) ILIKE %s ORDER BY power_mw DESC NULLS LAST", (f"%{name_q.lower()}%",))
            if row:
                return row, tbl
    return None, None


def _capacity_for_market(cur, market_hint, state_hint):
    """Aggregate pipeline rows by market (preferred) or state."""
    if market_hint:
        cur.execute(
            """SELECT operator, market, capacity_mw, phase, status,
                      completion_date, notes
                 FROM capacity_pipeline
                WHERE market ILIKE %s
                ORDER BY capacity_mw DESC NULLS LAST LIMIT 25""",
            (f"%{market_hint}%",))
    elif state_hint:
        cur.execute(
            """SELECT operator, market, capacity_mw, phase, status,
                      completion_date, notes
                 FROM capacity_pipeline
                WHERE market ILIKE %s
                ORDER BY capacity_mw DESC NULLS LAST LIMIT 25""",
            (f"%{state_hint}%",))
    else:
        return []
    rows = cur.fetchall()
    return [
        {"operator": r[0], "market": r[1],
         "capacity_mw": _as_float(r[2]),
         "phase": r[3], "status": r[4],
         "completion_date": r[5].isoformat() if r[5] and hasattr(r[5], 'isoformat') else r[5],
         "notes": (r[6] or '')[:200]}
        for r in rows
    ]


def _dcpi_for_market(cur, city, state):
    """If the site's city or state matches a DCPI-scored market, return
    that market's verdict + scores."""
    if not (city or state):
        return None
    candidates = []
    if city:
        candidates.append(city.lower().replace(' ', '-'))
        candidates.append(city.lower().split(',')[0].strip().replace(' ', '-'))
    if state:
        candidates.append(state.lower())
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    try:
        cur.execute(
            """SELECT DISTINCT ON (market_slug)
                      market_slug, market_name, iso, verdict,
                      excess_power_score, constraint_score,
                      time_to_power_months, computed_at
                 FROM market_power_scores
                WHERE LOWER(market_slug) = ANY(%s)
                   OR LOWER(state) = ANY(%s)
                ORDER BY market_slug, computed_at DESC
                LIMIT 1""",
            (candidates, candidates))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "market_slug": row[0], "market_name": row[1], "iso": row[2],
        "verdict": row[3],
        "excess_power_score": _as_float(row[4]),
        "constraint_score": _as_float(row[5]),
        "time_to_power_months": _as_float(row[6]),
        "computed_at": row[7].isoformat() if row[7] else None,
    }


def _peer_facilities(cur, city, state, exclude_id):
    """Other facilities in the same city/state — instant comparables."""
    if not city and not state:
        return []
    if city:
        cur.execute(
            """SELECT id, name, provider, power_mw, status
                 FROM facilities
                WHERE LOWER(city) = LOWER(%s)
                  AND id <> %s
                ORDER BY power_mw DESC NULLS LAST LIMIT 6""",
            (city, exclude_id or 0))
    else:
        cur.execute(
            """SELECT id, name, provider, power_mw, status
                 FROM facilities
                WHERE UPPER(state) = UPPER(%s)
                  AND id <> %s
                ORDER BY power_mw DESC NULLS LAST LIMIT 6""",
            (state, exclude_id or 0))
    return [
        {"id": r[0], "name": r[1], "provider": r[2],
         "power_mw": _as_float(r[3]), "status": r[4]}
        for r in cur.fetchall()
    ]


def _news_for_site(cur, name, provider, city):
    """Best-effort news search keyed off the site's name/provider/city."""
    needles = [n for n in [name, provider, city] if n]
    if not needles:
        return []
    where = " OR ".join(["title ILIKE %s OR body ILIKE %s"] * len(needles))
    params = []
    for n in needles:
        params.extend([f"%{n}%", f"%{n}%"])
    try:
        cur.execute(
            f"""SELECT title, url, published_date, source
                  FROM news
                 WHERE {where}
                 ORDER BY published_date DESC NULLS LAST
                 LIMIT 5""", params)
        rows = cur.fetchall()
    except Exception:
        return []
    return [
        {"title": r[0], "url": r[1],
         "published_date": r[2].isoformat() if r[2] and hasattr(r[2], 'isoformat') else r[2],
         "source": r[3]}
        for r in rows
    ]


@sites_capacity_bp.route("/api/v1/sites/<ident>/capacity-report", methods=["GET"])
def capacity_report(ident):
    """Bundled per-site capacity report. ident = numeric id, slug, or
    fuzzy name. Pure DB reads, one connection, best-effort per section."""
    try:
        with _conn() as c, c.cursor() as cur:
            row, source_table = _resolve_site(cur, ident)
            if not row:
                return jsonify(ok=False, error="site_not_found",
                               hint="pass an integer id, slug, or facility name"), 404

            (sid, name, provider, city, state, country,
             lat, lon, status, power_mw, src, first_seen) = row

            site = {
                "id": sid, "name": name, "provider": provider,
                "city": city, "state": state, "country": country,
                "latitude": _as_float(lat), "longitude": _as_float(lon),
                "status": status, "power_mw": _as_float(power_mw),
                "source": src, "source_table": source_table,
                "first_seen": first_seen.isoformat() if first_seen and hasattr(first_seen, 'isoformat') else first_seen,
            }

            # Capacity rollup for this market (operator-agnostic — the
            # site is part of the market's total pipeline).
            pipeline = _capacity_for_market(cur, city, state)
            pipeline_mw = sum(p.get("capacity_mw", 0) or 0 for p in pipeline)
            under_construction_mw = sum(
                (p.get("capacity_mw") or 0) for p in pipeline
                if 'construct' in (p.get("phase") or p.get("status") or '').lower())
            planned_mw = sum(
                (p.get("capacity_mw") or 0) for p in pipeline
                if not ('construct' in (p.get("phase") or p.get("status") or '').lower()
                        or 'operational' in (p.get("phase") or p.get("status") or '').lower()))
            operational_mw = _as_float(power_mw) or 0
            total = operational_mw + pipeline_mw
            utilization = round(operational_mw / total * 100, 1) if total else None

            capacity = {
                "operational_mw": operational_mw,
                "under_construction_mw": round(under_construction_mw, 1),
                "planned_mw": round(planned_mw, 1),
                "total_mw_market": round(total, 1),
                "utilization_pct": utilization,
            }

            dcpi = _dcpi_for_market(cur, city, state)
            peers = _peer_facilities(cur, city, state, sid)
            news = _news_for_site(cur, name, provider, city)

        return jsonify(
            ok=True,
            site=site,
            capacity=capacity,
            pipeline=pipeline,
            dcpi=dcpi,
            peer_facilities=peers,
            news=news,
            drill_deeper={
                "market_brief": "/api/v1/brief/market?market=<slug>",
                "dcpi_market": "/api/v1/dcpi/scores/<slug>",
                "fiber_routes": "/api/v1/fiber/routes",
                "water_risk":   "/api/v1/water/stress",
            },
            note=("One-call site brief — facility metadata + capacity rollup "
                  "for the market + DCPI verdict + peer facilities + news. "
                  "Use drill_deeper paths for per-domain depth."),
            generated_at=datetime.now(timezone.utc).isoformat(),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200
