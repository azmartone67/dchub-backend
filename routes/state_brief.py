"""
state_brief.py — Per-state Data Center Market Brief.

Roll-up of every DCPI market in a state into ONE shareable brief.
Captures broad SEO queries like "texas data center market" — a state-
level aggregation nobody else publishes. Same pattern as
routes/market_brief.py (one-connection fan-out, best-effort per section,
6h edge cache, tier-paywalled).

Endpoints:
  GET /states/<state>/brief       — HTML page (8 seed states pre-warmed)
  GET /api/v1/state-brief/<state> — JSON of same data

State slug grammar:
  - 2-letter lowercase  → tx, ca, va, ga, oh, or, il, az
  - full lowercase name → texas, california, virginia, georgia, ohio,
                          oregon, illinois, arizona
Both forms resolve to the same canonical full name (see STATE_ALIASES).

Sections (anon vs PRO+):
  ✓ Hero                  — FREE  : name, aggregate verdict, total ops MW
  ✓ KPIs                  — FREE  : markets tracked, facilities, ops MW,
                                    pipeline MW, dominant ISO, median lease
  ✓ Top Markets In State  — FREE  : ranked list w/ verdicts + click-through
  ✓ Outlook (teaser)      — FREE  : aggregate read (80-word teaser for anon)
  • ISO Breakdown         — PRO+ : per-ISO ops MW + queue depth
  • Transmission Build-Out — PRO+ : planned + under-construction lines
  • State Policy/Incentives — PRO+ : tax_incentives_neon summary
  • Top Operators In State — PRO+ : top 10 by MW within state
  • Recent M&A (24mo)     — PRO+ : any deal touching a facility in state

Reuses market_brief._is_pro() + _caller_tier() for the gate.
"""

from __future__ import annotations

import datetime
import os
import re
from urllib.parse import quote_plus

from flask import Blueprint, Response, jsonify, request

state_brief_bp = Blueprint("state_brief", __name__)

# ─────────────────────────────────────────────────────────────────────
# State registry
# ─────────────────────────────────────────────────────────────────────

# Seed states — initial 8. Beyond these the surface auto-renders for any
# state with at least one market_power_scores row in it, but only the
# seed eight are sitemapped + pre-warmed by the cron.
SEED_STATES = (
    "texas",
    "california",
    "virginia",
    "georgia",
    "ohio",
    "oregon",
    "illinois",
    "arizona",
)

# Lowercase full-name → (USPS code, Title-case name) lookup. Used both
# to canonicalize incoming slugs AND to drive the per-state DB queries
# that expect a 2-letter state code (transmission_lines.state,
# tax_incentives_neon.state_abbr) vs a Title-case name (water_risk.state).
STATE_INFO: dict[str, tuple[str, str]] = {
    "alabama":              ("AL", "Alabama"),
    "alaska":               ("AK", "Alaska"),
    "arizona":              ("AZ", "Arizona"),
    "arkansas":             ("AR", "Arkansas"),
    "california":           ("CA", "California"),
    "colorado":             ("CO", "Colorado"),
    "connecticut":          ("CT", "Connecticut"),
    "delaware":             ("DE", "Delaware"),
    "florida":              ("FL", "Florida"),
    "georgia":              ("GA", "Georgia"),
    "hawaii":               ("HI", "Hawaii"),
    "idaho":                ("ID", "Idaho"),
    "illinois":             ("IL", "Illinois"),
    "indiana":              ("IN", "Indiana"),
    "iowa":                 ("IA", "Iowa"),
    "kansas":               ("KS", "Kansas"),
    "kentucky":             ("KY", "Kentucky"),
    "louisiana":            ("LA", "Louisiana"),
    "maine":                ("ME", "Maine"),
    "maryland":             ("MD", "Maryland"),
    "massachusetts":        ("MA", "Massachusetts"),
    "michigan":             ("MI", "Michigan"),
    "minnesota":            ("MN", "Minnesota"),
    "mississippi":          ("MS", "Mississippi"),
    "missouri":             ("MO", "Missouri"),
    "montana":              ("MT", "Montana"),
    "nebraska":             ("NE", "Nebraska"),
    "nevada":               ("NV", "Nevada"),
    "new-hampshire":        ("NH", "New Hampshire"),
    "new-jersey":           ("NJ", "New Jersey"),
    "new-mexico":           ("NM", "New Mexico"),
    "new-york":             ("NY", "New York"),
    "north-carolina":       ("NC", "North Carolina"),
    "north-dakota":         ("ND", "North Dakota"),
    "ohio":                 ("OH", "Ohio"),
    "oklahoma":             ("OK", "Oklahoma"),
    "oregon":               ("OR", "Oregon"),
    "pennsylvania":         ("PA", "Pennsylvania"),
    "rhode-island":         ("RI", "Rhode Island"),
    "south-carolina":       ("SC", "South Carolina"),
    "south-dakota":         ("SD", "South Dakota"),
    "tennessee":            ("TN", "Tennessee"),
    "texas":                ("TX", "Texas"),
    "utah":                 ("UT", "Utah"),
    "vermont":              ("VT", "Vermont"),
    "virginia":             ("VA", "Virginia"),
    "washington":           ("WA", "Washington"),
    "west-virginia":        ("WV", "West Virginia"),
    "wisconsin":            ("WI", "Wisconsin"),
    "wyoming":              ("WY", "Wyoming"),
    "district-of-columbia": ("DC", "District of Columbia"),
}

# Alias: 2-letter lowercase code OR variant → canonical full-name slug.
# Built off STATE_INFO so adding a state needs one edit, not two.
STATE_ALIASES: dict[str, str] = {}
for _full, (_abbr, _) in STATE_INFO.items():
    STATE_ALIASES[_abbr.lower()] = _full  # tx → texas
    STATE_ALIASES[_full] = _full          # texas → texas (no-op)
# Hand-picked variants
STATE_ALIASES["nyc"] = "new-york"
STATE_ALIASES["dc"] = "district-of-columbia"


# ─────────────────────────────────────────────────────────────────────
# Helpers (mirror market_brief.py for consistency)
# ─────────────────────────────────────────────────────────────────────

def _conn():
    """One read-only Postgres connection, short connect timeout."""
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _norm_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")


def _canonical_state(slug: str) -> str | None:
    """Resolve any accepted slug (tx, texas, TX) to the canonical full-name
    slug. Returns None for unknown."""
    s = _norm_slug(slug)
    return STATE_ALIASES.get(s)


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Reuse the canonical tier resolver from market_brief — same paywall semantics.
def _caller_tier() -> str:
    try:
        from routes.market_brief import _caller_tier as _mb_tier
        return _mb_tier()
    except Exception:
        return "FREE"


def _is_pro(tier: str) -> bool:
    try:
        from routes.market_brief import _is_pro as _mb_is_pro
        return _mb_is_pro(tier)
    except Exception:
        return (tier or "").upper() in (
            "PRO", "FOUNDING", "ENTERPRISE", "RESEARCH_SEED", "ADMIN")


# ─────────────────────────────────────────────────────────────────────
# Section fan-out — each best-effort
# ─────────────────────────────────────────────────────────────────────

def _section_markets(cur, abbr: str, full_name: str) -> list[dict]:
    """Section: every market_power_scores row in this state, with verdict.
    Used for both the hero verdict-rollup AND the Top Markets In State
    table. Excludes _CAUTION-only rows from the BUILD/AVOID majority vote
    but still surfaces them in the table."""
    try:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, state, iso, verdict,
                   excess_power_score, constraint_score, computed_at
              FROM market_power_scores
             WHERE UPPER(COALESCE(state, '')) = UPPER(%s)
                OR LOWER(COALESCE(state, '')) = LOWER(%s)
             ORDER BY market_slug, computed_at DESC
        """, (abbr, full_name))
        rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        constraint = float(r[6] or 0)
        excess = float(r[5] or 0)
        v = (r[4] or "").upper()
        if v not in ("BUILD", "CAUTION", "AVOID"):
            # Mirror market_brief._section_hero verdict derivation.
            if constraint == 0 and excess == 0:
                v = "NODATA"
            elif excess >= 60 and constraint <= 40:
                v = "BUILD"
            elif excess >= 50 and constraint <= 50:
                v = "BUILD"
            elif constraint >= 70 and excess <= 40:
                v = "AVOID"
            elif constraint >= 60 and excess <= 30:
                v = "AVOID"
            else:
                v = "CAUTION"
        out.append({
            "slug":             r[0],
            "name":             r[1],
            "state":            r[2],
            "iso":              r[3],
            "verdict":          v,
            "excess_power":     _as_float(r[5]),
            "constraint_score": _as_float(r[6]),
            "computed_at":      r[7].isoformat() if r[7] else None,
            "brief_url":        f"/markets/{r[0]}/brief",
        })
    # Sort: BUILD first, then CAUTION, then AVOID, then NODATA — within
    # each verdict by excess_power DESC.
    rank = {"BUILD": 0, "CAUTION": 1, "AVOID": 2, "NODATA": 3}
    out.sort(key=lambda m: (rank.get(m["verdict"], 9), -(m["excess_power"] or 0)))
    return out


def _aggregate_verdict(markets: list[dict]) -> str:
    """Pick the dominant verdict by simple count. Ties → CAUTION."""
    if not markets:
        return "NODATA"
    counts = {"BUILD": 0, "CAUTION": 0, "AVOID": 0, "NODATA": 0}
    for m in markets:
        counts[m.get("verdict") or "NODATA"] = counts.get(
            m.get("verdict") or "NODATA", 0) + 1
    # Strict majority (>50%) wins; otherwise plurality of BUILD/AVOID
    # if one exceeds CAUTION, else CAUTION as the safe centroid.
    total = sum(counts.values())
    if not total:
        return "NODATA"
    sorted_v = sorted(counts.items(), key=lambda x: -x[1])
    top, top_n = sorted_v[0]
    if top_n / total > 0.5:
        return top
    if counts["BUILD"] > counts["CAUTION"] and counts["BUILD"] >= counts["AVOID"]:
        return "BUILD"
    if counts["AVOID"] > counts["CAUTION"] and counts["AVOID"] > counts["BUILD"]:
        return "AVOID"
    return "CAUTION"


def _section_kpis(cur, abbr: str, full_name: str, markets: list[dict]) -> dict:
    """Section: aggregate KPIs across the state."""
    out = {
        "markets_tracked":     len(markets),
        "total_facilities":    None,
        "total_operational_mw": None,
        "total_pipeline_mw":   None,
        "dominant_iso":        None,
        "median_lease_rate":   None,
    }
    market_names = [m.get("name") for m in markets if m.get("name")]
    if not market_names:
        return out
    # Facilities + MW aggregated across the state's markets. Best-effort —
    # state column on discovered_facilities is per-row, market is per-row.
    try:
        # Use either market match OR state match — discovered_facilities
        # has both columns; one might be sparser than the other.
        cur.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(power_mw), 0),
                   COALESCE(SUM(CASE WHEN status ILIKE %s OR status ILIKE %s
                                     THEN power_mw ELSE 0 END), 0)
              FROM discovered_facilities
             WHERE (UPPER(COALESCE(state, '')) = UPPER(%s)
                    OR LOWER(COALESCE(state, '')) = LOWER(%s))
               AND merged_at IS NULL AND is_duplicate = 0
        """, ('%construction%', '%planned%', abbr, full_name))
        r = cur.fetchone() or (None, None, None)
        out["total_facilities"]     = _as_int(r[0])
        out["total_operational_mw"] = _as_float(r[1])
        out["total_pipeline_mw"]    = _as_float(r[2])
    except Exception:
        pass
    # Dominant ISO by ops MW. The ISO is on market_power_scores; aggregate
    # at the market-name level by joining facility MW to the market's iso.
    try:
        iso_counts: dict[str, float] = {}
        for m in markets:
            iso = m.get("iso")
            if not iso:
                continue
            try:
                cur.execute("""
                    SELECT COALESCE(SUM(power_mw), 0)
                      FROM discovered_facilities
                     WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
                       AND merged_at IS NULL AND is_duplicate = 0
                """, (m.get("name") or "",))
                rr = cur.fetchone()
                if rr and rr[0]:
                    iso_counts[iso] = iso_counts.get(iso, 0) + float(rr[0])
            except Exception:
                continue
        if iso_counts:
            top_iso, top_mw = max(iso_counts.items(), key=lambda kv: kv[1])
            out["dominant_iso"] = {
                "iso":       top_iso,
                "mw_share":  _as_float(top_mw),
                "all_isos":  [{"iso": k, "mw": _as_float(v)}
                              for k, v in sorted(iso_counts.items(),
                                                 key=lambda kv: -kv[1])],
            }
    except Exception:
        pass
    # Median lease rate — best-effort from powered_shell_pricing or similar.
    try:
        cur.execute("""
            SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lease_rate)
              FROM powered_shell_pricing
             WHERE UPPER(COALESCE(state, '')) = UPPER(%s)
               AND lease_rate IS NOT NULL
        """, (abbr,))
        r = cur.fetchone()
        if r and r[0] is not None:
            out["median_lease_rate"] = _as_float(r[0])
    except Exception:
        pass
    return out


def _section_iso_breakdown(cur, full_name: str, markets: list[dict]) -> list[dict]:
    """Section (PRO+): per-ISO operational MW + queue depth in this state.
    Useful when a state straddles ISOs (e.g. CA has CAISO + LADWP; TX has
    ERCOT + tiny SPP slivers)."""
    by_iso: dict[str, dict] = {}
    for m in markets:
        iso = m.get("iso")
        if not iso:
            continue
        if iso not in by_iso:
            by_iso[iso] = {"iso": iso, "markets": [], "operational_mw": 0.0,
                           "queue_capacity_mw": None}
        by_iso[iso]["markets"].append(m.get("name"))
    # operational MW per market → roll up per ISO
    for iso, agg in by_iso.items():
        total = 0.0
        for name in agg["markets"]:
            try:
                cur.execute("""
                    SELECT COALESCE(SUM(power_mw), 0)
                      FROM discovered_facilities
                     WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
                       AND merged_at IS NULL AND is_duplicate = 0
                """, (name or "",))
                r = cur.fetchone()
                if r and r[0]:
                    total += float(r[0])
            except Exception:
                continue
        agg["operational_mw"] = _as_float(total)
        # Queue capacity per ISO — best-effort.
        try:
            cur.execute("""
                SELECT COALESCE(SUM(capacity_mw), 0)
                  FROM interconnection_queue
                 WHERE UPPER(COALESCE(iso, '')) = UPPER(%s)
                   AND (LOWER(COALESCE(status, '')) LIKE '%%pending%%'
                        OR LOWER(COALESCE(status, '')) LIKE '%%active%%'
                        OR LOWER(COALESCE(status, '')) LIKE '%%study%%')
            """, (iso,))
            r = cur.fetchone()
            if r and r[0]:
                agg["queue_capacity_mw"] = _as_float(r[0])
        except Exception:
            pass
        # Render-friendly market count instead of full list
        agg["market_count"] = len(agg["markets"])
        agg["markets"] = agg["markets"][:5]
    return sorted(by_iso.values(),
                  key=lambda x: -(x.get("operational_mw") or 0))


def _section_policy(cur, abbr: str) -> dict | None:
    """Section (PRO+): state tax incentives. Reuses the
    tax_incentives_neon schema from site_brief._tax_for_state."""
    if not abbr:
        return None
    try:
        cur.execute("""
            SELECT state_name, sales_tax_exempt, property_tax_abatement,
                   data_center_specific, qualifying_investment,
                   incentive_details
              FROM tax_incentives_neon
             WHERE state_abbr = %s LIMIT 1
        """, (abbr.upper(),))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    summary = row[5]
    if summary and len(summary) > 480:
        summary = summary[:480] + "…"
    return {
        "state_name":             row[0],
        "sales_tax_exempt":       row[1],
        "property_tax_abatement": row[2],
        "data_center_specific":   row[3],
        "qualifying_investment":  row[4],
        "summary":                summary,
    }


def _section_transmission(cur, abbr: str) -> dict:
    """Section (PRO+): planned + under-construction transmission projects
    in this state. transmission_lines.state is the canonical USPS code."""
    out = {
        "total_lines":      None,
        "planned_lines":    None,
        "planned_miles":    None,
        "by_voltage":       [],
        "top_planned":      [],
    }
    if not abbr:
        return out
    try:
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (
                       WHERE status ILIKE '%%planned%%'
                          OR status ILIKE '%%proposed%%'
                          OR status ILIKE '%%construction%%'
                   ),
                   COALESCE(SUM(length_miles) FILTER (
                       WHERE status ILIKE '%%planned%%'
                          OR status ILIKE '%%proposed%%'
                          OR status ILIKE '%%construction%%'
                   ), 0)
              FROM transmission_lines
             WHERE UPPER(state) = UPPER(%s)
        """, (abbr,))
        r = cur.fetchone()
        if r:
            out["total_lines"]   = _as_int(r[0])
            out["planned_lines"] = _as_int(r[1])
            out["planned_miles"] = _as_float(r[2])
    except Exception:
        pass
    # By voltage class — rough buckets so the page can show kV-density.
    try:
        cur.execute("""
            SELECT CASE
                     WHEN voltage_kv >= 500 THEN '500+ kV'
                     WHEN voltage_kv >= 345 THEN '345 kV'
                     WHEN voltage_kv >= 230 THEN '230 kV'
                     WHEN voltage_kv >= 138 THEN '138 kV'
                     WHEN voltage_kv >  0   THEN '<138 kV'
                     ELSE 'unknown'
                   END AS bucket,
                   COUNT(*) AS n,
                   COALESCE(SUM(length_miles), 0) AS miles
              FROM transmission_lines
             WHERE UPPER(state) = UPPER(%s)
             GROUP BY bucket
             ORDER BY n DESC
             LIMIT 6
        """, (abbr,))
        out["by_voltage"] = [
            {"bucket": r[0], "lines": _as_int(r[1]), "miles": _as_float(r[2])}
            for r in cur.fetchall()
        ]
    except Exception:
        pass
    # Top planned/proposed lines (highest kV first)
    try:
        cur.execute("""
            SELECT name, operator, voltage_kv, length_miles, status, from_sub, to_sub
              FROM transmission_lines
             WHERE UPPER(state) = UPPER(%s)
               AND (status ILIKE '%%planned%%'
                    OR status ILIKE '%%proposed%%'
                    OR status ILIKE '%%construction%%')
             ORDER BY voltage_kv DESC NULLS LAST, length_miles DESC NULLS LAST
             LIMIT 10
        """, (abbr,))
        out["top_planned"] = [{
            "name":         r[0],
            "operator":     r[1],
            "voltage_kv":   _as_float(r[2]),
            "length_miles": _as_float(r[3]),
            "status":       r[4],
            "from_sub":     r[5],
            "to_sub":       r[6],
        } for r in cur.fetchall()]
    except Exception:
        pass
    return out


def _section_operators(cur, abbr: str, full_name: str) -> list[dict]:
    """Section (PRO+): top 10 operators by MW within state boundaries."""
    try:
        cur.execute("""
            SELECT provider,
                   COUNT(*) AS n,
                   COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE (UPPER(COALESCE(state, '')) = UPPER(%s)
                    OR LOWER(COALESCE(state, '')) = LOWER(%s))
               AND provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider
             ORDER BY mw DESC NULLS LAST, n DESC
             LIMIT 10
        """, (abbr, full_name))
        return [{
            "operator":       r[0],
            "facility_count": _as_int(r[1]),
            "total_mw":       _as_float(r[2]),
        } for r in cur.fetchall()]
    except Exception:
        return []


def _section_ma(cur, abbr: str, full_name: str,
                market_names: list[str]) -> list[dict]:
    """Section (PRO+): deals in the last 24 months touching any facility
    in the state. Match by deal.region/market against state name OR any
    of the state's market names."""
    try:
        # Use ANY(%s::text[]) so we can match across N market names plus
        # state name in a single query.
        candidates = []
        for n in market_names:
            if n:
                candidates.append(n.lower())
        candidates.append(full_name.lower())
        candidates.append(abbr.lower())
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, asset_name
              FROM deals
             WHERE (LOWER(COALESCE(market, '')) = ANY(%s)
                    OR LOWER(COALESCE(region, '')) = ANY(%s))
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
             ORDER BY date DESC NULLS LAST
             LIMIT 20
        """, (candidates, candidates))
        return [{
            "date":   d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer":  d[1], "seller": d[2],
            "value":  _as_float(d[3]),
            "mw":     _as_float(d[4]),
            "type":   d[5],
            "asset":  d[6],
        } for d in cur.fetchall()]
    except Exception:
        # Fallback to a per-market name scan — slower but works against
        # deals tables that don't accept ANY() against text.
        out = []
        for name in market_names + [full_name]:
            if not name:
                continue
            try:
                cur.execute("""
                    SELECT date, buyer, seller, value, mw, type, asset_name
                      FROM deals
                     WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
                       AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
                     ORDER BY date DESC NULLS LAST LIMIT 5
                """, (name,))
                out.extend({
                    "date":   d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
                    "buyer":  d[1], "seller": d[2],
                    "value":  _as_float(d[3]),
                    "mw":     _as_float(d[4]),
                    "type":   d[5],
                    "asset":  d[6],
                } for d in cur.fetchall())
            except Exception:
                continue
        return out[:20]


def _section_outlook(markets: list[dict], kpis: dict,
                     full_name: str) -> dict:
    """Section: 12-month aggregate outlook. Sums the per-market
    market_deep_dive narratives where they exist; falls back to a
    template otherwise."""
    out = {"narrative_md": None, "word_count": 0,
           "generated_at": None, "verdict": None,
           "rationale": None}
    # Try to pull a few short deep-dive paragraphs and concatenate.
    paragraphs = []
    try:
        from routes.market_deep_dive import read_deep_dive
        for m in markets[:3]:
            slug = m.get("slug")
            if not slug:
                continue
            try:
                r = read_deep_dive(slug)
            except Exception:
                r = None
            if not r:
                continue
            narrative = (r.get("narrative_md") or "").strip()
            if not narrative:
                continue
            # First paragraph only — keeps the outlook readable.
            first = narrative.split("\n\n")[0].strip()
            if first:
                paragraphs.append(f"**{m.get('name')}:** {first}")
    except Exception:
        pass
    if paragraphs:
        out["narrative_md"] = "\n\n".join(paragraphs)
        out["word_count"] = sum(len(p.split()) for p in paragraphs)
        out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        out["rationale"] = "Aggregated from per-market deep-dive narratives."
    else:
        verdict = _aggregate_verdict(markets)
        ops_mw = kpis.get("total_operational_mw")
        pipe_mw = kpis.get("total_pipeline_mw")
        n = kpis.get("markets_tracked") or 0
        out["narrative_md"] = (
            f"**12-month outlook: {verdict}.** {full_name} hosts "
            f"{n} DCPI-tracked market{'s' if n != 1 else ''} totaling "
            f"{int(ops_mw):,} MW operational and {int(pipe_mw):,} MW pipeline. "
            "Per-market deep-dive narratives are generated nightly by Claude; "
            "the aggregate outlook here updates within 24h of any individual "
            "market verdict shift."
        ) if (ops_mw is not None and pipe_mw is not None) else (
            f"**12-month outlook: {verdict}.** {full_name} hosts "
            f"{n} DCPI-tracked market{'s' if n != 1 else ''}. "
            "Aggregate facility data is loading on the next cron pass."
        )
        out["rationale"] = "Auto-template (per-market deep-dives not yet generated)."
        out["word_count"] = len(out["narrative_md"].split())
    out["verdict"] = _aggregate_verdict(markets)
    return out


def _live_as_of(cur, abbr: str) -> dict:
    """REAL source-table age — youngest of (market_power_scores.computed_at
    for any market in state, discovered_facilities.last_seen for state)."""
    youngest = None
    try:
        cur.execute("""
            SELECT MAX(computed_at)
              FROM market_power_scores
             WHERE UPPER(COALESCE(state, '')) = UPPER(%s)
        """, (abbr,))
        r = cur.fetchone()
        if r and r[0]:
            youngest = r[0]
    except Exception:
        pass
    try:
        cur.execute("""
            SELECT MAX(COALESCE(last_seen, first_seen))
              FROM discovered_facilities
             WHERE UPPER(COALESCE(state, '')) = UPPER(%s)
        """, (abbr,))
        r = cur.fetchone()
        if r and r[0]:
            if youngest is None or r[0] > youngest:
                youngest = r[0]
    except Exception:
        pass
    if youngest is None:
        return {"iso": None, "age_hours": None}
    age_h = None
    try:
        now = datetime.datetime.utcnow()
        ts = youngest
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        age_h = round((now - ts).total_seconds() / 3600.0, 1)
    except Exception:
        pass
    return {
        "iso":        youngest.isoformat() if hasattr(youngest, "isoformat") else str(youngest),
        "age_hours":  age_h,
    }


# ─────────────────────────────────────────────────────────────────────
# Build the full brief
# ─────────────────────────────────────────────────────────────────────

def _build_brief(state: str, tier: str) -> dict:
    """Run the per-section fan-out in ONE connection."""
    canonical = _canonical_state(state)
    requested_slug = _norm_slug(state)
    out: dict = {
        "ok":          False,
        "state":       canonical,
        "requested":   requested_slug,
        "redirect_to": canonical if (canonical and canonical != requested_slug) else None,
        "tier":        tier,
        "is_pro":      _is_pro(tier),
    }
    if not canonical:
        out["error"] = "state_not_found"
        out["sample_states"] = sorted(SEED_STATES)
        return out

    abbr, full_name = STATE_INFO[canonical]
    out["state_code"] = abbr
    out["state_name"] = full_name

    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    is_pro = out["is_pro"]
    try:
        with c.cursor() as cur:
            markets = _section_markets(cur, abbr, full_name)
            if not markets:
                out["error"] = "state_not_in_coverage"
                # Surface seed states so the caller can retry.
                out["sample_states"] = sorted(SEED_STATES)
                return out
            kpis = _section_kpis(cur, abbr, full_name, markets)
            aggregate_verdict = _aggregate_verdict(markets)
            live = _live_as_of(cur, abbr)

            out["hero"] = {
                "name":              full_name,
                "code":              abbr,
                "verdict":           aggregate_verdict,
                "operational_mw":    kpis.get("total_operational_mw"),
                "pipeline_mw":       kpis.get("total_pipeline_mw"),
                "markets_tracked":   kpis.get("markets_tracked"),
                "dominant_iso":      (kpis.get("dominant_iso") or {}).get("iso"),
            }
            out["live_as_of"] = live
            out["kpis"]       = kpis
            # Always FREE: ranked Top Markets table (the headline value)
            out["top_markets"] = markets

            # Outlook — FREE teaser, PRO+ full
            out["outlook"] = _section_outlook(markets, kpis, full_name)

            if is_pro:
                out["iso_breakdown"] = _section_iso_breakdown(cur, full_name, markets)
                out["policy"]        = _section_policy(cur, abbr)
                out["transmission"]  = _section_transmission(cur, abbr)
                out["operators"]     = _section_operators(cur, abbr, full_name)
                market_names = [m.get("name") for m in markets if m.get("name")]
                out["ma"]            = _section_ma(cur, abbr, full_name, market_names)
            else:
                out["iso_breakdown"] = []
                out["policy"]        = None
                out["transmission"]  = None
                out["operators"]     = []
                out["ma"]            = []
                out["paywall"] = {
                    "required_tier": "PRO",
                    "checkout_url":  "/pricing?utm_source=state_brief",
                    "blurb": ("ISO Breakdown, Transmission Build-Out, State "
                              "Policy / Tax Incentives, Top Operators, and "
                              "M&A activity are unlocked for PRO subscribers "
                              "— $499/mo, all states."),
                }
        out["ok"] = True
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────

_VERDICT_PALETTE = {
    "BUILD":   {"pill_bg": "#10b981", "pill_fg": "#06251b"},
    "CAUTION": {"pill_bg": "#f59e0b", "pill_fg": "#28190a"},
    "AVOID":   {"pill_bg": "#ef4444", "pill_fg": "#280808"},
    "NODATA":  {"pill_bg": "#71717a", "pill_fg": "#fafafa"},
}


def _verdict_colors(v: str | None) -> dict:
    return _VERDICT_PALETTE.get((v or "").upper(),
                                _VERDICT_PALETTE["NODATA"])


def _fmt_mw(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.0f} MW"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v):
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(v):
    if v is None:
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _render_html(brief: dict) -> str:
    """Render the 9-section HTML, inline-styled, matching market_brief look."""
    state = brief.get("state") or ""
    full_name = brief.get("state_name") or state.replace("-", " ").title()
    hero = brief.get("hero") or {}
    live = brief.get("live_as_of") or {}
    kpis = brief.get("kpis") or {}
    outlook = brief.get("outlook") or {}
    is_pro = bool(brief.get("is_pro"))
    top_markets = brief.get("top_markets") or []

    verdict = hero.get("verdict") or "—"
    colors = _verdict_colors(verdict)
    live_iso = live.get("iso") or ""
    live_age = live.get("age_hours")
    live_age_str = f"{live_age:.1f}h" if isinstance(live_age, (int, float)) else "—"
    citation_iso = (live_iso or "")[:19].replace("T", " ")

    # ── KPI tiles ────────────────────────────────────────────────────
    dominant = (kpis.get("dominant_iso") or {}).get("iso") or "—"
    kpi_tiles = [
        ("Markets Tracked", _fmt_int(kpis.get("markets_tracked"))),
        ("Total Facilities", _fmt_int(kpis.get("total_facilities"))),
        ("Operational",     _fmt_mw(kpis.get("total_operational_mw"))),
        ("Pipeline",        _fmt_mw(kpis.get("total_pipeline_mw"))),
        ("Dominant ISO",    dominant),
    ]
    if kpis.get("median_lease_rate") is not None:
        kpi_tiles.append(("Median Lease",
                          f"${kpis['median_lease_rate']:.2f}/kW-mo"))
    kpi_html = "\n".join(
        f'<div class="kpi"><span class="kpi-l">{lab}</span>'
        f'<span class="kpi-v">{val}</span></div>'
        for lab, val in kpi_tiles)

    # ── Top Markets table (FREE) ──────────────────────────────────────
    def _market_row(m):
        v = m.get("verdict") or "—"
        c = _verdict_colors(v)
        return (
            f'<tr><td><a href="{m.get("brief_url") or "#"}">{m.get("name") or "—"}</a></td>'
            f'<td><span class="pill-sm" style="background:{c["pill_bg"]};color:{c["pill_fg"]}">{v}</span></td>'
            f'<td>{m.get("iso") or "—"}</td>'
            f'<td>{m.get("excess_power") if m.get("excess_power") is not None else "—"}</td>'
            f'<td>{m.get("constraint_score") if m.get("constraint_score") is not None else "—"}</td>'
            f'</tr>'
        )
    top_rows = "\n".join(_market_row(m) for m in top_markets) or (
        '<tr><td colspan="5">No DCPI markets tracked yet</td></tr>')
    top_html = (
        '<table><thead><tr><th>Market</th><th>Verdict</th>'
        '<th>ISO</th><th>Excess Power</th><th>Constraint</th></tr></thead>'
        f'<tbody>{top_rows}</tbody></table>'
    )

    # ── Blur card (PRO teaser) ────────────────────────────────────────
    blur = (
        '<div class="blur-card">'
        '<div class="blur-overlay">'
        '<div class="blur-title">PRO unlocks all sections</div>'
        '<div class="blur-body">ISO Breakdown · Transmission Build-Out · '
        'Tax Incentives · Top Operators · M&amp;A — all states, live updates, '
        'share-ready.</div>'
        '<a class="cta" href="/pricing?utm_source=state_brief">Unlock with PRO — $499/mo</a>'
        '</div>'
        '<div class="blur-fake">'
        '<div class="fake-row"></div><div class="fake-row"></div>'
        '<div class="fake-row"></div><div class="fake-row"></div>'
        '</div></div>'
    )

    # ── PRO sections ──────────────────────────────────────────────────
    if is_pro:
        # ISO Breakdown
        iso_breakdown = brief.get("iso_breakdown") or []
        if iso_breakdown:
            iso_rows = "\n".join(
                f'<tr><td>{r.get("iso") or "—"}</td>'
                f'<td>{_fmt_mw(r.get("operational_mw"))}</td>'
                f'<td>{_fmt_mw(r.get("queue_capacity_mw"))}</td>'
                f'<td>{r.get("market_count") or 0}</td></tr>'
                for r in iso_breakdown)
            iso_html = (
                '<table><thead><tr><th>ISO</th><th>Operational MW</th>'
                '<th>Queue Capacity</th><th>Markets</th></tr></thead>'
                f'<tbody>{iso_rows}</tbody></table>')
        else:
            iso_html = '<p class="sub">No ISO mapping yet for this state.</p>'

        # Transmission
        trans = brief.get("transmission") or {}
        top_planned = trans.get("top_planned") or []
        if top_planned:
            t_rows = "\n".join(
                f'<tr><td>{r.get("name") or "—"}</td>'
                f'<td>{r.get("operator") or "—"}</td>'
                f'<td>{r.get("voltage_kv") if r.get("voltage_kv") is not None else "—"} kV</td>'
                f'<td>{r.get("length_miles") if r.get("length_miles") is not None else "—"} mi</td>'
                f'<td>{r.get("status") or "—"}</td></tr>'
                for r in top_planned)
            t_table = (
                '<table><thead><tr><th>Project</th><th>Operator</th>'
                '<th>Voltage</th><th>Length</th><th>Status</th></tr></thead>'
                f'<tbody>{t_rows}</tbody></table>')
        else:
            t_table = '<p class="sub">No planned transmission projects tracked.</p>'
        t_summary = (
            f'<div class="grid3">'
            f'<div class="cell"><b>Total Lines</b><span>{_fmt_int(trans.get("total_lines"))}</span></div>'
            f'<div class="cell"><b>Planned / In-Construction</b><span>{_fmt_int(trans.get("planned_lines"))}</span></div>'
            f'<div class="cell"><b>Planned Miles</b><span>{trans.get("planned_miles") if trans.get("planned_miles") is not None else "—"}</span></div>'
            f'</div>'
        )
        transmission_html = t_summary + t_table

        # Policy
        policy = brief.get("policy") or {}
        if policy:
            policy_html = (
                f'<div class="grid3">'
                f'<div class="cell"><b>Sales Tax Exempt</b><span>{"Yes" if policy.get("sales_tax_exempt") else "No"}</span></div>'
                f'<div class="cell"><b>Property Tax Abatement</b><span>{"Yes" if policy.get("property_tax_abatement") else "No"}</span></div>'
                f'<div class="cell"><b>DC-Specific Statute</b><span>{"Yes" if policy.get("data_center_specific") else "No"}</span></div>'
                f'<div class="cell"><b>Min Investment</b><span>{policy.get("qualifying_investment") or "—"}</span></div>'
                f'</div>'
                f'<p class="sub">{policy.get("summary") or "No incentive summary on file."}</p>'
            )
        else:
            policy_html = '<p class="sub">No tax incentive data on file for this state.</p>'

        # Operators
        ops = brief.get("operators") or []
        if ops:
            op_rows = "\n".join(
                f'<tr><td>{o.get("operator") or "—"}</td>'
                f'<td>{_fmt_int(o.get("facility_count"))}</td>'
                f'<td>{_fmt_mw(o.get("total_mw"))}</td></tr>'
                for o in ops)
            operators_html = (
                '<table><thead><tr><th>Operator</th>'
                '<th>Facilities</th><th>Total MW</th></tr></thead>'
                f'<tbody>{op_rows}</tbody></table>')
        else:
            operators_html = '<p class="sub">No operator data tracked yet.</p>'

        # M&A
        ma = brief.get("ma") or []
        if ma:
            ma_rows = "\n".join(
                f'<tr><td>{m.get("date") or "—"}</td>'
                f'<td>{m.get("buyer") or "—"}</td>'
                f'<td>{m.get("seller") or "—"}</td>'
                f'<td>{_fmt_money(m.get("value"))}</td>'
                f'<td>{_fmt_mw(m.get("mw"))}</td></tr>'
                for m in ma)
            ma_html = (
                '<table><thead><tr><th>Date</th><th>Buyer</th>'
                '<th>Seller</th><th>Value</th><th>MW</th></tr></thead>'
                f'<tbody>{ma_rows}</tbody></table>')
        else:
            ma_html = '<p class="sub">No M&amp;A activity in the last 24 months.</p>'
    else:
        iso_html = blur
        transmission_html = blur
        policy_html = blur
        operators_html = blur
        ma_html = blur

    # ── Outlook ──────────────────────────────────────────────────────
    narrative = outlook.get("narrative_md") or ""
    if not is_pro:
        words = narrative.split()
        narrative = " ".join(words[:80])
        if len(words) > 80:
            narrative += "… <em>(continued for PRO subscribers)</em>"
    out_paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in narrative.split("\n\n") if p.strip()
    ) or "<p>Aggregate outlook will be generated on the next cron pass.</p>"

    # ── Share + citation ──────────────────────────────────────────────
    page_url = f"https://dchub.cloud/states/{state}/brief"
    # 2026-06-07 page-audit: safer encoding (handles multi-word state names
    # like "New Mexico" + special chars in verdict). Same fix as market_brief.py.
    _share_text = f"{full_name} data center market brief — verdict {verdict}"
    share_x = (f"https://twitter.com/intent/tweet?text={quote_plus(_share_text)}"
               f"&url={quote_plus(page_url)}")
    share_li = f"https://www.linkedin.com/sharing/share-offsite/?url={page_url}"
    citation = (f'DC Hub · <a href="{page_url}">{page_url}</a> · '
                f"Live as of {citation_iso} UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{full_name} Data Center Market Brief · DC Hub</title>
<meta name="description" content="{full_name} data center market — aggregate DCPI verdict {verdict}, {kpis.get('markets_tracked') or '—'} tracked markets, total operational MW + pipeline + ISO breakdown, transmission build-out, tax incentives, operators, M&amp;A, 12-month outlook.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{page_url}">
<meta property="og:title" content="{full_name} Data Center Market · DC Hub">
<meta property="og:description" content="Aggregate DCPI verdict {verdict} · Live as of {citation_iso} UTC.">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:960px;margin:0 auto;padding:2.5rem 1.25rem;background:var(--bg);color:#d4d4d8;line-height:1.65;-webkit-font-smoothing:antialiased}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2.4rem;color:var(--tx)}}
h2{{font-size:1.25rem;font-weight:600;color:var(--tx);margin:2.25rem 0 .75rem;letter-spacing:-.01em}}
.live-pill{{display:inline-flex;align-items:center;gap:.4rem;background:var(--surf);border:1px solid var(--b);border-radius:999px;padding:.35rem .8rem;font-size:.72rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin-left:.5rem}}
.live-dot{{width:.5rem;height:.5rem;background:#10b981;border-radius:50%;animation:pulse 2.5s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.verdict-pill{{display:inline-block;background:{colors['pill_bg']};color:{colors['pill_fg']};font-weight:700;font-size:.85rem;padding:.45rem 1rem;border-radius:8px;letter-spacing:.04em;text-transform:uppercase}}
.pill-sm{{display:inline-block;font-weight:600;font-size:.7rem;padding:.2rem .5rem;border-radius:5px;letter-spacing:.04em;text-transform:uppercase}}
.sub{{color:var(--dim);font-size:.85rem;margin:0 0 1.5rem;font-family:'JetBrains Mono',monospace}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem;margin:1rem 0 2rem}}
.kpi{{background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:.95rem 1.1rem;display:flex;flex-direction:column;gap:.35rem}}
.kpi-l{{font-size:.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
.kpi-v{{font-size:1.35rem;color:var(--tx);font-weight:600;letter-spacing:-.01em}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.6rem;margin:.5rem 0 1.5rem}}
.cell{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.75rem .95rem;display:flex;justify-content:space-between;align-items:center;font-size:.88rem}}
.cell b{{color:var(--dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;font-weight:500;font-family:'JetBrains Mono',monospace}}
.cell span{{color:var(--tx);font-weight:600;font-family:'JetBrains Mono',monospace}}
table{{width:100%;border-collapse:collapse;margin:.5rem 0 1.5rem;font-size:.88rem;background:var(--surf);border:1px solid var(--b);border-radius:10px;overflow:hidden}}
th,td{{padding:.55rem .85rem;text-align:left;border-bottom:1px solid var(--b)}}
th{{background:rgba(255,255,255,0.025);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);font-family:'JetBrains Mono',monospace;font-weight:500}}
td{{color:var(--tx);font-family:'JetBrains Mono',monospace}}
td a{{color:var(--ind);text-decoration:none}}
tbody tr:last-child td{{border-bottom:none}}
.outlook p{{font-size:1.02rem;margin:1rem 0;color:#d4d4d8}}
.share{{display:flex;gap:.5rem;flex-wrap:wrap;margin:2rem 0}}
.share a{{background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.5rem 1rem;color:var(--ind);text-decoration:none;font-size:.85rem;font-family:'JetBrains Mono',monospace}}
.share a:hover{{border-color:var(--ind)}}
.blur-card{{position:relative;background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:2rem;margin:.5rem 0 1.5rem;overflow:hidden;min-height:180px}}
.blur-overlay{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,10,15,.4) 0%,rgba(10,10,15,.92) 100%);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:1.5rem;z-index:2;backdrop-filter:blur(6px)}}
.blur-title{{font-size:1.05rem;font-weight:600;color:var(--tx);margin-bottom:.5rem}}
.blur-body{{font-size:.85rem;color:var(--mut);max-width:380px;margin-bottom:1.25rem}}
.cta{{background:var(--grad);color:#fff;text-decoration:none;padding:.65rem 1.4rem;border-radius:8px;font-weight:600;font-size:.9rem;letter-spacing:.01em}}
.blur-fake{{filter:blur(8px);opacity:.45}}
.fake-row{{height:1.5rem;background:linear-gradient(90deg,#27272a 0%,#3f3f46 50%,#27272a 100%);margin:.5rem 0;border-radius:6px}}
.citation{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.85rem 1rem;font-size:.78rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin:2rem 0 1rem}}
.citation a{{color:var(--ind);text-decoration:none}}
.footer{{color:var(--dim);font-size:.78rem;margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.footer a{{color:var(--ind);text-decoration:none}}
</style>
</head>
<body>

<h1>{full_name}<span class="live-pill"><span class="live-dot"></span>Live · {live_age_str} fresh</span></h1>
<p class="sub">State Data Center Market · <span class="verdict-pill">{verdict}</span> · Live as of {citation_iso} UTC</p>

<h2>State KPIs</h2>
<div class="kpis">
{kpi_html}
</div>

<h2>Top Markets in {full_name}</h2>
{top_html}

<h2>ISO Breakdown</h2>
{iso_html}

<h2>Transmission Build-Out</h2>
{transmission_html}

<h2>State Policy &amp; Incentives</h2>
{policy_html}

<h2>Top Operators in {full_name}</h2>
{operators_html}

<h2>Recent M&amp;A (24mo)</h2>
{ma_html}

<h2>12-Month Outlook</h2>
<div class="outlook">
{out_paragraphs}
</div>

<div class="share">
  <a href="{share_x}" target="_blank" rel="noopener">Share on X</a>
  <a href="{share_li}" target="_blank" rel="noopener">Share on LinkedIn</a>
  <a href="#" onclick="navigator.clipboard.writeText('{page_url}');this.textContent='Copied!';return false;">Copy URL</a>
</div>

<div class="citation">{citation}</div>

<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a> · Source-of-truth data center market intelligence · JSON: <a href="/api/v1/state-brief/{state}">/api/v1/state-brief/{state}</a></p>

<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# Pre-warm — called by crawler_scheduler._run_state_brief_warm
# ─────────────────────────────────────────────────────────────────────

def prewarm_seed_states() -> dict:
    """Pre-build the brief for each seed state. Best-effort — never raises.
    1s sleep between states per the dchub-backend-flapping memory."""
    import time as _time
    out: dict = {"warmed": 0, "errors": 0, "states": []}
    n = len(SEED_STATES)
    for i, st in enumerate(SEED_STATES):
        try:
            brief = _build_brief(st, tier="ADMIN")
            ok = bool(brief.get("ok"))
            out["states"].append({"state": st, "ok": ok,
                                  "error": brief.get("error")})
            if ok:
                out["warmed"] += 1
            else:
                out["errors"] += 1
        except Exception as e:
            out["errors"] += 1
            out["states"].append({"state": st, "ok": False,
                                  "error": f"{type(e).__name__}:{str(e)[:80]}"})
        if i < n - 1:
            try:
                _time.sleep(1.0)
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@state_brief_bp.route("/api/v1/state-brief/<state>", methods=["GET"])
def api_state_brief(state):
    """JSON of the state brief."""
    tier = _caller_tier()
    brief = _build_brief(state, tier)
    if brief.get("ok"):
        status = 200
    elif brief.get("error") in ("state_not_found", "state_not_in_coverage"):
        status = 404
    else:
        status = 200
    resp = jsonify(brief)
    # 6h edge cache
    resp.headers["Cache-Control"] = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, status


@state_brief_bp.route("/states/<state>/brief", methods=["GET"])
def html_state_brief(state):
    """HTML render — 9 sections, paywalled by tier."""
    tier = _caller_tier()
    brief = _build_brief(state, tier)
    # Always-200 for emailed links — render a fallback shell on missing.
    if not brief.get("ok") and brief.get("error") in (
            "state_not_found", "state_not_in_coverage"):
        sample = brief.get("sample_states") or list(SEED_STATES)
        sample_html = ", ".join(
            f'<a href="/states/{s}/brief">{s}</a>' for s in sample[:8])
        body = (
            f'<h1>State Brief — {state}</h1>'
            f'<p class="sub">This state is not yet in our DCPI coverage. '
            f'Try one of: {sample_html}</p>'
            f'<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a></p>'
        )
        return Response(
            f"<!doctype html><html><head><meta charset=utf-8>"
            f"<title>State Brief · DC Hub</title>"
            f'<link rel="stylesheet" href="/static/dchub-brand.css"></head>'
            f"<body>{body}</body></html>",
            mimetype="text/html",
            headers={"Cache-Control": "public, max-age=300"})
    if brief.get("redirect_to") and brief["redirect_to"] != state:
        from flask import redirect
        return redirect(f"/states/{brief['redirect_to']}/brief", code=301)
    html = _render_html(brief)
    return Response(html, mimetype="text/html", headers={
        # 6h edge cache
        "Cache-Control":       "public, max-age=21600, s-maxage=21600",
        "X-State-Brief-Tier":  tier,
    })
