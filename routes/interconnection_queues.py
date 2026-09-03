"""Phase ZZZZZ-round47 (2026-05-25) — Interconnection queue tracker.

Public landing page + JSON API + JSON-LD Dataset schema so AI engines
treat dchub.cloud as the citable reference for queue numbers. Source
data: iso_queue_snapshots (cron-populated; currently seeded from public
2026-Q1 disclosures).

Wiring (main.py):
    from routes.interconnection_queues import interconnection_queues_bp
    app.register_blueprint(interconnection_queues_bp)

Endpoints:
  GET /interconnection-queues                — HTML landing page (SEO + JSON-LD)
  GET /api/v1/interconnection-queue/snapshot — full snapshot, all ISOs
  GET /api/v1/interconnection-queue/by-iso?iso=ERCOT — single-ISO detail
"""
import os, json, math
from datetime import datetime, timezone
from flask import Blueprint, jsonify, Response, request
from routes._slow_tool_cache import cache_tool_response
import psycopg
# ★ Fail-soft, matching this module's own `try: import requests` convention.
# tests/test_cross_layer_public_reason_hygiene.py execs the route with ALL
# first-party imports BLOCKED so every degraded branch is deterministic — a
# hard top-level import breaks that harness. When the helper is unavailable we
# publish "unknown", which the vocabulary already defines as "read it
# defensively". An absent-or-honest shape beats a guessed one.
try:
    from util.constraint_coverage_shape import shape_of as _shape_of
except Exception:                                    # pragma: no cover
    _shape_of = None


def _cc_shape(value):
    return _shape_of(value) if _shape_of else "unknown"

interconnection_queues_bp = Blueprint("interconnection_queues", __name__)


# ── Phase 3 parcel geometry (self-contained; no shapely/PostGIS needed) ──────
# analyze_parcel operates on ANY caller-provided GeoJSON — the interface ships
# ahead of DC Hub owning a parcel-boundary dataset (Regrid/assessor). The queue
# handoff won't carry `geometry` until that data is sourced; this lets an agent
# that already HAS a polygon get DC Hub's structured read today.
_ACRES_PER_M2 = 1.0 / 4046.8564224

def _ring_area_m2(ring):
    """Geodesic area of a ring [[lng,lat],...] via the spherical-excess formula."""
    R = 6378137.0
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total * R * R / 2.0)

def _ring_centroid(ring):
    """Planar (shoelace) centroid of a ring [[lng,lat],...] -> [lng, lat]. Good to
    a few metres at parcel scale; falls back to the vertex mean for degenerate rings."""
    n = len(ring)
    if n < 3:
        return [sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n] if n else [0.0, 0.0]
    A = cx = cy = 0.0
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cross = x0 * y1 - x1 * y0
        A += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(A) < 1e-12:
        return [sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n]
    A *= 0.5
    return [cx / (6 * A), cy / (6 * A)]

def _members_from_geometry(geom):
    """Extract each member's OUTER ring from a GeoJSON Polygon / MultiPolygon."""
    if not isinstance(geom, dict):
        return []
    t = (geom.get("type") or "").lower()
    coords = geom.get("coordinates") or []
    members = []
    try:
        if t == "polygon" and coords:
            members.append(coords[0])
        elif t == "multipolygon":
            for poly in coords:
                if poly:
                    members.append(poly[0])
    except Exception:
        return []
    return [r for r in members if isinstance(r, list) and len(r) >= 3]
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _latest_snapshot():
    if not NEON_URL: return []
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
              SELECT iso, as_of, queued_load_total_gw, queued_load_data_center_gw,
                     queued_load_dc_share_pct, new_applications_q_gw, new_applications_period,
                     historical_completion_pct, top_subregions, source_url, source_name
              FROM iso_queue_snapshots
              WHERE (iso, as_of) IN (
                SELECT iso, MAX(as_of) FROM iso_queue_snapshots GROUP BY iso
              )
              ORDER BY queued_load_total_gw DESC NULLS LAST
            """)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _serialize(r):
    return {
        "iso": r["iso"],
        "as_of": r["as_of"].isoformat() if r["as_of"] else None,
        "queued_load_total_gw": float(r["queued_load_total_gw"] or 0),
        # Honest-null: these feeds are GENERATION queues and do NOT classify
        # queued load as data-center. NULL means "not tracked" — coercing it to
        # 0.0 (the old `or 0`) fabricated a "0.0 GW DC-load" figure. Use
        # `is not None` so a genuine 0.0 (if a feed ever provides one) survives.
        "queued_load_data_center_gw": float(r["queued_load_data_center_gw"]) if r["queued_load_data_center_gw"] is not None else None,
        "queued_load_dc_share_pct": float(r["queued_load_dc_share_pct"]) if r["queued_load_dc_share_pct"] is not None else None,
        "new_applications_q_gw": float(r["new_applications_q_gw"]) if r["new_applications_q_gw"] else None,
        "new_applications_period": r["new_applications_period"],
        "historical_completion_pct": float(r["historical_completion_pct"]) if r["historical_completion_pct"] else None,
        "top_subregions": r["top_subregions"],
        "source_url": r["source_url"],
        "source_name": r["source_name"],
        # provenance-v1 (2026-07-11): every iso_queue_snapshots row is the
        # ISO's OWN published disclosure (source_name/source_url above) —
        # never a DC Hub inference. Compact per-record flag.
        "v": "published",
    }


# r-queue-projects (2026-06-20): per-PROJECT drill-down. The snapshot above is
# GW aggregates from iso_queue_snapshots; this surfaces the 5,300+ NAMED projects
# loaded into interconnect_queue (project, MW, status, state/county) — the actual
# "which data-center / generation builds are in the queue" data agents ask for.
# Ordered by capacity_mw so the biggest builds lead. lat/lng are NULL (the ISO
# feeds carry no coordinates); location is state/county.
def _top_projects(iso=None, limit=8):
    if not NEON_URL:
        return []
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            if iso:
                cur.execute("""
                  SELECT project_name, iso, state, county, fuel_type, capacity_mw,
                         queue_status, queue_date, queue_id
                  FROM interconnect_queue WHERE upper(iso) = upper(%s)
                  ORDER BY capacity_mw DESC NULLS LAST LIMIT %s
                """, (iso, limit))
            else:
                cur.execute("""
                  SELECT project_name, iso, state, county, fuel_type, capacity_mw,
                         queue_status, queue_date, queue_id
                  FROM interconnect_queue
                  ORDER BY capacity_mw DESC NULLS LAST LIMIT %s
                """, (limit,))
            cols = [c.name for c in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["capacity_mw"] = float(d["capacity_mw"]) if d["capacity_mw"] is not None else None
                d["queue_date"] = d["queue_date"].isoformat() if d.get("queue_date") else None
                out.append(d)
            return out
    except Exception:
        return []


def _project_counts():
    """Per-ISO row counts + total in interconnect_queue (one cheap query)."""
    if not NEON_URL:
        return {}, 0
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT upper(iso), COUNT(*) FROM interconnect_queue GROUP BY upper(iso)")
            by = {iso: int(n) for iso, n in cur.fetchall()}
            return by, sum(by.values())
    except Exception:
        return {}, 0


def _snapshot_provenance(as_of=None, *, default_v):
    """provenance-v1 collection block for the queue surfaces. Fail-soft:
    returns None on any error (jsonify serializes None harmlessly).

    default_v (v1 lock, REQUIRED): the tier a record without ``v`` inherits.
    snapshot/by-iso rows are pure ISO disclosures → "published"; the refined
    endpoint mixes in DC Hub enrichments → "inferred"."""
    try:
        from routes.provenance import provenance_block
        return provenance_block(
            source=("US ISO public interconnection queues (ERCOT GIS / PJM "
                    "NSQ / MISO GI / SPP / CAISO / NYISO / ISO-NE)"),
            method=("published ISO queue disclosures, ingested per-ISO; "
                    "per-record v: published = the ISO's own figure, "
                    "inferred = DC Hub derivation"),
            as_of=as_of,
            default_v=default_v,
        )
    except Exception:
        return None


# shell#35 (2026-07-26): latest dchub_classified DC-queue series written by
# depth_master_shell._act_large_load (category dc_load_queue_measured).
# Fail-soft readers — snapshot must never 500 on a missing table/rows.
def _dchub_classified_by_iso() -> dict:
    try:
        from db_utils import get_connection
        conn = get_connection()
    except Exception:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (iso) iso, primary_value, as_of
                  FROM grid_ext_metrics
                 WHERE category = 'dc_load_queue_measured'
                   AND primary_value IS NOT NULL
                 ORDER BY iso, as_of DESC NULLS LAST
            """)
            return {r[0]: {"gw": float(r[1]),
                           "as_of": r[2].isoformat() if r[2] else None}
                    for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _dchub_classified_total():
    by = _dchub_classified_by_iso()
    if not by:
        return None
    return round(sum(v["gw"] for v in by.values()), 2)


@interconnection_queues_bp.route("/api/v1/interconnection-queue/snapshot")
def api_snapshot():
    snap = _latest_snapshot()
    _proj_by, _proj_total = _project_counts()      # r-queue-projects: per-project drill-down
    _proj_top = _top_projects(None, 10)
    # r47.1: Postgres NUMERIC -> Decimal; cast to float BEFORE arithmetic
    # so we don't mix float/Decimal (TypeError otherwise).
    total_gw = float(sum((r["queued_load_total_gw"] or 0) for r in snap))
    # Honest-null: sum only the rows that actually carry a DC figure; if none do
    # (the current reality — generation queues don't classify DC load), the DC
    # total + share are null, NOT a fabricated 0.0.
    _dc_vals = [r["queued_load_data_center_gw"] for r in snap
                if r["queued_load_data_center_gw"] is not None]
    dc_gw    = float(sum(_dc_vals)) if _dc_vals else None
    # r70 (2026-06-03): per-ISO freshness. Snapshots refresh per-ISO, so the set
    # can mix today's live data (MISO/SPP/CAISO/NYISO real parsers) with older
    # seeded rows (ERCOT/PJM/ISO-NE, whose source URLs moved). Surface it honestly
    # so the headline total is never mistaken for uniformly-fresh — and use the
    # LATEST date as the top-line as_of (was snap[0], an arbitrary single ISO).
    _dates = [r["as_of"] for r in snap if r.get("as_of")]
    _latest = max(_dates) if _dates else None
    _fresh = sum(1 for r in snap if r.get("as_of") == _latest)
    return jsonify({
        "as_of": _latest.isoformat() if _latest else None,
        "iso_count": len(snap),
        "totals": {
            "queued_load_gw": total_gw,
            "queued_load_data_center_gw": dc_gw,
            "dc_share_pct": round(100.0 * dc_gw / total_gw, 1) if (dc_gw is not None and total_gw) else None,
            # shell#35: DC Hub's own row-level classification (independent of
            # ISO-published large-load figures) — per-ISO in dchub_classified.
            "dchub_classified_dc_gw": _dchub_classified_total(),
        },
        "dchub_classified": {
            "by_iso": _dchub_classified_by_iso(),
            "method": ("Conservative classifier over the tracked per-project "
                       "queue rows: fuel_type=Load OR DC name-match "
                       "(data center/hyperscale/colocation/compute campus). "
                       "Independent of, and comparable to, ISO-published "
                       "large-load totals."),
        },
        "data_center_load": {
            "tracked": False,
            "note": ("Figures cover the full generation interconnection queue. The ISO "
                     "public queues (ERCOT GIS / PJM NSQ / MISO GI / SPP / CAISO / NYISO / "
                     "ISO-NE) are generation-side and do not classify queued load as "
                     "data-center, so per-ISO data-center load is not derivable from these "
                     "feeds and is reported as null rather than 0."),
        },
        "freshness": {
            "latest_as_of": _latest.isoformat() if _latest else None,
            "fresh_iso_count": _fresh,
            "stale_iso_count": len(snap) - _fresh,
            "note": "Per-ISO as_of is in by_iso; 'fresh' = refreshed on the latest daily ingest.",
        },
        "by_iso": [_serialize(r) for r in snap],
        "projects": {
            "tracked": _proj_total > 0,
            "total": _proj_total,
            "by_iso_count": _proj_by,
            "top": _proj_top,
            "note": ("Named per-project queue rows (project, MW, status, state/county) "
                     "from the 7 US ISO public queues. Aggregate GW totals are in "
                     "by_iso; call /api/v1/interconnection-queue/by-iso?iso=ERCOT for "
                     "the full per-ISO project list."),
        },
        "methodology": "DCPI maps ISO queue position + load growth vs signed contracts -> Excess Power / Constraint scoring. Per-ISO BUILD/CAUTION/AVOID verdicts at https://dchub.cloud/dcpi.",
        "source": "https://dchub.cloud/interconnection-queues",
        # provenance-v1 (2026-07-11): collection block. Per-record v on each
        # by_iso row: published = the ISO's own disclosed figure (source_name/
        # source_url per row); inferred would mark a DC Hub derivation.
        "provenance": _snapshot_provenance(_latest, default_v="published"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }), 200, {
        # r47.4 (2026-05-25): CF Pages was caching the earlier 500-then-404
        # responses on this exact URL. no-store forces revalidation on every
        # request so future errors don't get pinned at the edge cache.
        "Cache-Control": "no-store, must-revalidate",
        "X-DC-Phase": "ZZZZZ-round47-snapshot",
    }


@interconnection_queues_bp.route("/api/v1/interconnection-queue/by-iso")
def api_by_iso():
    iso = (request.args.get("iso") or "").upper().strip()
    if not iso:
        return jsonify({"error": "iso required"}), 400
    snap = [r for r in _latest_snapshot() if r["iso"].upper() == iso]
    if not snap:
        return jsonify({"error": "iso_not_found", "iso": iso,
                        "available": [r["iso"] for r in _latest_snapshot()]}), 404
    out = _serialize(snap[0])
    # r-queue-projects (2026-06-20): attach the named per-project rows for this ISO
    # (top 25 by MW) from interconnect_queue — the drill-down the GW aggregate lacks.
    out["projects"] = _top_projects(iso, 25)
    out["project_count"] = _project_counts()[0].get(iso, 0)
    out["provenance"] = _snapshot_provenance(snap[0].get("as_of"),
                                             default_v="published")
    return jsonify(out)


def _row_html(r):
    subs = r["top_subregions"] or []
    parts = []
    for s in subs[:5]:
        verdict = (s.get("dcpi_verdict") or "caution").lower()
        parts.append(
            f'<span class="name">{s.get("name","?")}</span> '
            f'({s.get("queued_gw","?")}GW, {s.get("ttp_months","?")}mo) '
            f'<span class="dcpi-{verdict}">{s.get("dcpi_verdict","?")}</span>'
        )
    sub_html = " &middot; ".join(parts)
    return (
        f"<tr>"
        f'<td class="iso">{r["iso"]}</td>'
        f"<td>{float(r['queued_load_total_gw'] or 0):.1f}</td>"
        f"<td>{'—' if r['queued_load_data_center_gw'] is None else format(float(r['queued_load_data_center_gw']), '.1f')}</td>"
        f'<td class="dc-share">{"—" if r["queued_load_dc_share_pct"] is None else format(float(r["queued_load_dc_share_pct"]), ".0f") + "%"}</td>'
        f'<td class="subregions">{sub_html}</td>'
        f"</tr>"
    )


@interconnection_queues_bp.route("/interconnection-queues")
def landing():
    snap = _latest_snapshot()
    # r47.1: cast Decimal -> float before arithmetic (same bug as api_snapshot)
    total_gw = float(sum((r["queued_load_total_gw"] or 0) for r in snap))
    # Honest-null: DC load is NOT classified in these generation queues.
    _dc_vals = [r["queued_load_data_center_gw"] for r in snap if r["queued_load_data_center_gw"] is not None]
    dc_gw    = float(sum(_dc_vals)) if _dc_vals else None
    dc_share = round(100.0 * dc_gw / total_gw, 0) if (dc_gw is not None and total_gw) else None
    # Display strings — render '—' / a plain-English note rather than a fake 0.
    _dc_tracked    = dc_gw is not None
    _dc_gw_card    = f"{int(dc_gw):,} GW" if _dc_tracked else "—"
    _dc_share_card = f"{int(dc_share)}%" if (_dc_tracked and dc_share is not None) else "—"
    _dc_clause     = (f"{int(dc_share)}% is tied to data centers and AI clusters"
                      if (_dc_tracked and dc_share is not None)
                      else "data-center share is not separately classified in these generation-queue feeds")
    _dc_clause_short = (f"{int(dc_share)}% data centers" if (_dc_tracked and dc_share is not None)
                        else "DC share not separately tracked")

    jsonld = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "ISO Interconnection Queue Snapshots (Data Center Focus)",
        "description": f"Per-ISO interconnection queue MW totals + top BUILD subregions. {int(total_gw)} GW total large-load queued across {len(snap)} US ISOs as of 2026-Q1; {_dc_clause}.",
        "url": "https://dchub.cloud/interconnection-queues",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "dateModified": datetime.now(timezone.utc).date().isoformat(),
        "distribution": [{
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": "https://dchub.cloud/api/v1/interconnection-queue/snapshot",
        }],
        "isAccessibleForFree": True,
        "license": "https://dchub.cloud/terms",
        "keywords": "data center, interconnection queue, ERCOT, PJM, MISO, AI load, large load, DCPI",
    }

    rows_html = "".join(_row_html(r) for r in snap)

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>US ISO Interconnection Queues &mdash; Data Center Tracker &middot; DC Hub</title>
<meta name="description" content="{int(total_gw):,} GW total large-load queued across {len(snap)} US ISOs ({_dc_clause_short}). Per-ISO BUILD/CAUTION/AVOID verdicts + top subregions with shortest TTP. Updated Q1 2026.">
<link rel="canonical" href="https://dchub.cloud/interconnection-queues">
<meta property="og:title" content="US ISO Interconnection Queues &mdash; Data Center Tracker">
<meta property="og:description" content="{int(total_gw)} GW queued &middot; {_dc_clause_short} &middot; per-ISO BUILD verdicts">
<meta property="og:url" content="https://dchub.cloud/interconnection-queues">
<meta property="og:type" content="website">
<link rel="alternate" type="application/json" href="/api/v1/interconnection-queue/snapshot" title="Snapshot JSON">
<link rel="alternate" type="application/mcp+json" href="https://dchub.cloud/mcp" title="DC Hub MCP">
<meta name="dchub:resource-type" content="interconnection-queue">
<meta name="dchub:mcp-tools" content="get_interconnection_queue,get_pipeline,analyze_site,get_intelligence_index">
<script type="application/ld+json">{json.dumps(jsonld, indent=2)}</script>
<style>
body{{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:32px 24px;color:#0f172a;background:#fafbfc}}
h1{{font-size:2rem;margin:0 0 12px;letter-spacing:-.01em}}
.eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:700;margin-bottom:10px}}
.lead{{color:#475569;font-size:1.05rem;margin-bottom:28px;max-width:760px}}
.headline-stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:24px 0 32px}}
.stat{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px}}
.stat-value{{font-size:1.8rem;font-weight:700;color:#0f172a;letter-spacing:-.02em}}
.stat-label{{font-size:.78rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin:18px 0;font-size:.92rem;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
th,td{{padding:11px 14px;text-align:left;border-bottom:1px solid #e2e8f0}}
th{{background:#f1f5f9;font-weight:600;font-size:.82rem;color:#475569;text-transform:uppercase;letter-spacing:.05em}}
tr:last-child td{{border-bottom:0}}
.iso{{font-family:ui-monospace,monospace;font-weight:700}}
.dc-share{{color:#6366f1;font-weight:600}}
.subregions{{font-size:.85rem;color:#475569}}
.subregions .name{{color:#0f172a;font-weight:500}}
.dcpi-build{{background:rgba(16,185,129,.12);color:#059669;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.dcpi-caution{{background:rgba(245,158,11,.12);color:#d97706;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.dcpi-avoid{{background:rgba(239,68,68,.12);color:#dc2626;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}}
.cta{{background:#0f172a;color:#fff;padding:24px 28px;border-radius:12px;margin:32px 0;display:flex;justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap}}
.cta h2{{font-size:1.15rem;margin:0 0 4px;color:#fff}}
.cta p{{color:#cbd5e1;margin:0;font-size:.9rem;max-width:560px}}
.cta a{{background:#6366f1;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;white-space:nowrap}}
.api-block{{background:#0f172a;color:#cbd5e1;padding:16px 20px;border-radius:10px;font-family:ui-monospace,monospace;font-size:.85rem;overflow-x:auto;margin:16px 0}}
.api-block .kw{{color:#7dd3fc}} .api-block .str{{color:#86efac}}
.methodology{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 22px;margin:24px 0;font-size:.92rem;color:#475569}}
.methodology h3{{margin:0 0 8px;font-size:1.05rem;color:#0f172a}}
.sources{{font-size:.78rem;color:#94a3b8;margin-top:24px}}
.sources a{{color:#6366f1;text-decoration:none}}
</style>
</head><body>
<div class="eyebrow">DCPI &middot; ISO Queue Tracker</div>
<h1>US Interconnection Queues &mdash; Data Center Load</h1>
<p class="lead">{int(total_gw):,} GW of large-load interconnection requests sit in active queues across {len(snap)} US ISOs as of Q1 2026 &mdash; {_dc_clause}. Below: per-ISO totals and top BUILD subregions (shorter queues, faster Time-to-Power).</p>
<div class="headline-stats">
  <div class="stat"><div class="stat-value">{int(total_gw):,} GW</div><div class="stat-label">Total queued large load</div></div>
  <div class="stat"><div class="stat-value">{_dc_gw_card}</div><div class="stat-label">Data-center load (not classified in gen-queue feeds)</div></div>
  <div class="stat"><div class="stat-value">{_dc_share_card}</div><div class="stat-label">DC % of queue</div></div>
  <div class="stat"><div class="stat-value">{len(snap)}</div><div class="stat-label">ISOs tracked</div></div>
</div>
<table>
<thead><tr><th>ISO</th><th>Total GW</th><th>DC GW</th><th>DC %</th><th>Top BUILD subregions (queued GW &middot; TTP months)</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class="cta">
  <div>
    <h2>Query this live from any AI agent</h2>
    <p>Available as the <code style="color:#fff">get_interconnection_queue</code> MCP tool. Add the DC Hub MCP server to Claude, Cursor, Cline, or any agent &mdash; then query queue data in any chat.</p>
  </div>
  <a href="https://dchub.cloud/integrations/mcp">Add MCP server &rarr;</a>
</div>
<div class="methodology">
  <h3>How DCPI uses queue data</h3>
  Queue position and length, combined with signed-contract load growth and retiring asset capacity, feed the DCPI <strong>Excess Power</strong> score (BUILD signal in markets where the queue is quiet relative to available headroom) and <strong>Constraint</strong> score (CAUTION/AVOID where backlog drives Time-to-Power past 17-24 months). Refreshed daily from the 7 major ISOs. <a href="/dcpi" style="color:#6366f1;font-weight:600">&rarr; Open DCPI</a>
</div>
<h3 style="margin-top:32px">JSON API</h3>
<div class="api-block">
<span class="kw">GET</span> <span class="str">/api/v1/interconnection-queue/snapshot</span>      <span style="color:#64748b"># all ISOs, latest</span><br>
<span class="kw">GET</span> <span class="str">/api/v1/interconnection-queue/by-iso?iso=ERCOT</span> <span style="color:#64748b"># single-ISO drill-down</span>
</div>
<p class="sources">
<strong>Sources</strong> (each ISO snapshot links its primary source):
<a href="https://www.ercot.com/gridinfo/resource">ERCOT MIS</a> &middot;
<a href="https://www.pjm.com/planning/services-requests/interconnection-queues">PJM Queue Tracker</a> &middot;
<a href="https://www.misoenergy.org/">MISO GIQ</a> &middot;
<a href="https://spp.org/engineering/generator-interconnection/">SPP DISIS</a> &middot;
<a href="https://www.caiso.com/generation-transmission/generation/generator-interconnection">CAISO</a> &middot;
<a href="https://www.nyiso.com/connecting-to-the-grid">NYISO</a> &middot;
<a href="https://www.iso-ne.com/system-planning/interconnection-service">ISO-NE</a> &middot;
LBNL <a href="https://emp.lbl.gov/queues">Queued Up</a>
</p>
</body></html>"""
    return Response(html, content_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600, s-maxage=3600",
                             "X-DC-Phase": "ZZZZZ-round47"})


# ── r-refined-queue (2026-07-06) — server-side set-reduction over the queue ──
# Gemini's architecture vote: push the high-cardinality filter to the DATA layer
# so an agent ingests survivors, not 1,744 GW of raw queue (the in-context-filter
# token-blowup). Phase-1 predicates all live on interconnect_queue (no spatial
# join): min_mw, max_ttp_months (ISO-level estimate), iso, baseload_only. A
# fiber_km predicate + a per-survivor site_evaluation_handoff arrive once the
# rows are geocoded (they carry state/county/POI but NULL lat/lng). Returns the
# envelope with _entity:"queue_results" so an agent branches before parsing.
_ISO_TTP_MONTHS = {  # ISO avg interconnection wait (DCPI); per-project ETA = geocode-era refinement
    "PJM": 50.5, "ERCOT": 32.6, "MISO": 33.7, "CAISO": 39.9,
    "SPP": 24.0, "NYISO": 31.4, "ISO-NE": 33.8, "ISONE": 33.8, "ISNE": 33.8,
}
# Firm / dispatchable = baseload-capable for AI load; exclude intermittent + storage-only.
# NOTE: Postgres POSIX regex uses \y for word boundary (not \b).
_NON_BASELOAD_RE = r"(wind|solar|\ypv\y|storage|battery|\ywin\y|\ybess\y)"
# status=active (default) = "still progressing through interconnection". ISOs label
# this differently (PJM/CAISO literal "active"; SPP "IA FULLY EXECUTED/ON SCHEDULE",
# "DISIS STAGE", "IA PENDING", ...), so a literal LIKE '%active%' silently zeroed SPP
# (the fastest-TTP ISO). Define active by EXCLUDING terminal states instead of matching
# a word: drop withdrawn/cancelled/terminated/suspended/online. Keep studies + IA-pending
# + on-schedule + under-construction. status=all skips the filter; any other value = LIKE.
_DEAD_STATUS_RE = r"(withdraw|cancel|terminat|suspend|commercial operation|deactivat)"


@interconnection_queues_bp.route("/api/v1/analyze-parcel", methods=["POST"])
def api_analyze_parcel():
    """Phase 3 interface: structured read of a GeoJSON parcel. Body:
    {"geometry": <GeoJSON Polygon|MultiPolygon>, "capacity_mw": <optional>}.
    Returns geodesic acreage, the largest-member centroid as representative_point,
    a contiguity flag + per-member breakdown, and a ready-to-pipe
    site_evaluation_handoff (analyze_site + get_water_risk at the anchor)."""
    body = request.get_json(silent=True) or {}
    geom = body.get("geometry") or {}
    capacity_mw = body.get("capacity_mw")
    try:
        capacity_mw = float(capacity_mw) if capacity_mw not in (None, "") else None
    except Exception:
        capacity_mw = None

    # r-parcel-lookup (2026-07-11): no geometry but a point? Serve the polygon
    # from the hosted parcel_boundaries layer (free county/state GIS pilot;
    # /api/v1/parcels/coverage lists live markets). Geometry callers unchanged;
    # point callers light up wherever hosted coverage exists.
    hosted_parcel = None
    if not geom:
        def _num_arg(*keys):
            for k in keys:
                v = body.get(k)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except Exception:
                        return None
            return None
        _lat, _lng = _num_arg("lat"), _num_arg("lng", "lon")
        if _lat is not None and _lng is not None:
            try:
                with psycopg.connect(NEON_URL, autocommit=True) as _c, _c.cursor() as _cur:
                    _cur.execute(
                        """SELECT parcel_id, county, state, acres_gis,
                                  ST_AsGeoJSON(geom, 6)
                           FROM parcel_boundaries
                           WHERE geom && ST_SetSRID(ST_MakePoint(%s,%s),4326)
                             AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326))
                           LIMIT 1""", (_lng, _lat, _lng, _lat))
                    _row = _cur.fetchone()
                if _row:
                    geom = json.loads(_row[4])
                    hosted_parcel = {
                        "parcel_id": _row[0], "county": _row[1], "state": _row[2],
                        "acres_per_source": _row[3], "hosted_lookup": True,
                        "data_basis": "county/state public GIS parcel layer hosted by DC Hub",
                    }
            except Exception:
                pass  # fail-soft: fall through to the geometry-required error
            if not geom:
                return jsonify(ok=False, _entity="error",
                               error="no hosted parcel contains that point — coverage is rolling out by market (GET /api/v1/parcels/coverage); pass an explicit GeoJSON geometry to analyze any boundary",
                               coverage_url="/api/v1/parcels/coverage"), 404

    members = _members_from_geometry(geom)
    if not members:
        return jsonify(ok=False, _entity="error",
                       error="geometry must be a GeoJSON Polygon or MultiPolygon with >=3-point outer rings — or pass lat+lng inside a hosted-coverage market (GET /api/v1/parcels/coverage)"), 400

    member_out = []
    for ring in members:
        cen = _ring_centroid(ring)  # [lng, lat]
        member_out.append({
            "acres": round(_ring_area_m2(ring) * _ACRES_PER_M2, 2),
            "centroid": {"lat": round(cen[1], 6), "lng": round(cen[0], 6)},
        })
    total_acres = round(sum(m["acres"] for m in member_out), 2)
    # representative_point = centroid of the LARGEST-area member — never the
    # multi-part geometric center, which can land off-parcel (highway median,
    # river) and poison every point-keyed read.
    largest = max(member_out, key=lambda m: m["acres"])
    rp = largest["centroid"]
    contiguous = len(member_out) == 1

    handoff = {
        "analyze_site": ({"lat": rp["lat"], "lon": rp["lng"], "capacity_mw": capacity_mw, "include_risk": True, "include_fiber": True}
                         if capacity_mw else {"lat": rp["lat"], "lon": rp["lng"], "include_risk": True, "include_fiber": True}),
        "get_water_risk": {"lat": rp["lat"], "lng": rp["lng"]},
    }

    return jsonify({
        "_entity": "parcel_analysis",
        "ok": True,
        **({"hosted_parcel": hosted_parcel} if hosted_parcel else {}),
        "representative_point": rp,
        "total_acres": total_acres,
        "member_count": len(member_out),
        "contiguous": contiguous,
        "members": member_out,
        "site_evaluation_handoff": handoff,
        # data_basis: first-class provenance so an agent knows how much to trust each
        # number (Grok's "modeled vs measured" point). analyze_parcel is MEASURED — it
        # computes ground truth from the exact vertices you pass; DC Hub adds no
        # positional error of its own. Contrast the queue handoff, where the anchor is
        # 'county_centroid' (approximate) vs 'poi_exact' — carried there as coordinate_precision.
        "data_basis": {
            "geometry_source": ("DC Hub hosted county/state GIS parcel layer (point lookup)"
                                if hosted_parcel else "caller-supplied WGS84 GeoJSON"),
            "area_method": "geodesic spherical-excess — measured, not modeled",
            "centroid_method": "planar shoelace of the largest-area member",
            "positional_error_added_by_dchub_m": 0,
        },
        "_source": "DC Hub — dchub.cloud",
        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"",
        "note": ("Geometry read for a GeoJSON Polygon/MultiPolygon. representative_point is the centroid "
                 "of the largest-area member (never the multi-part center, which can land off-parcel). "
                 "contiguous=false means members are DISCONTINUOUS — treat setbacks, fencing and "
                 "point-of-interconnection per-member, not as one summed footprint. Pipe "
                 "site_evaluation_handoff into analyze_site for grid/fiber/water/verdict at the anchor. "
                 "Acreage is geodesic (spherical-excess). Pass lat+lng with no geometry to look up the "
                 "containing parcel from DC Hub's hosted county/state GIS layer (rolling out by market — "
                 "GET /api/v1/parcels/coverage). get_refined_queue handoffs still never auto-carry "
                 "`geometry`: ISO queue rows have no parcel identity (proven 2026-07-06)."),
    })


# Objectives whose data is not yet in the stack. Declared EXPLICITLY so an agent
# gets "unavailable + reason" in constraint_coverage — never a silent skip, and
# never a fabricated proxy. (ChatGPT's "Constraint Coverage" pattern, 2026-07-07.)
_UNSUPPORTED_OBJECTIVES = {
    "water_stress": ("Awaiting WRI Aqueduct ingest — no direction-correct water-stress "
                     "data in the stack yet; the legacy groundwater proxy was inverted and is not surfaced."),
    "water_score": "Awaiting WRI Aqueduct ingest — see water_stress.",
    "water_risk": "Awaiting WRI Aqueduct ingest — see water_stress.",
    "water": "Awaiting WRI Aqueduct ingest — see water_stress.",
}

# 2026-07-08: the four water objectives are DATA-GATED, not code-gated. They stay
# "unavailable" until a VERIFIED WRI Aqueduct ingest lands real rows in water_risk
# (routes/water_aqueduct_ingest.py). The moment real WRI-sourced rows exist they
# drop out of the unsupported set and become scorable — with NO code change and,
# critically, NEVER off a fabricated proxy (the water_score pause of 2026-07-07
# holds until real data arrives). See [[reference_dchub_water_score_integrity]].
_WATER_OBJECTIVES = ("water_stress", "water_score", "water_risk", "water")
_water_avail_cache = {"ts": 0.0, "val": False}


def _wri_water_available() -> bool:
    """True only when water_risk carries REAL WRI-sourced rows. 5-min cache;
    fail-closed to False on any error (so an outage can never fabricate a
    water score by accident)."""
    import time as _t
    now = _t.time()
    if now - _water_avail_cache["ts"] < 300:
        return _water_avail_cache["val"]
    val = False
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as _c:
            cur = _c.cursor()
            cur.execute("SELECT 1 FROM water_risk "
                        "WHERE LOWER(COALESCE(source,'')) LIKE 'wri%%' LIMIT 1")
            val = cur.fetchone() is not None
    except Exception:
        val = False
    _water_avail_cache["ts"] = now
    _water_avail_cache["val"] = val
    return val


def _effective_unsupported() -> dict:
    """The unsupported-objectives set for THIS request. Water objectives become
    scorable the instant a verified WRI ingest lands — data-gated, no code flip."""
    if _wri_water_available():
        return {k: v for k, v in _UNSUPPORTED_OBJECTIVES.items()
                if k not in _WATER_OBJECTIVES}
    return _UNSUPPORTED_OBJECTIVES


@interconnection_queues_bp.route("/api/v1/rank-sites", methods=["POST"])
def api_rank_sites():
    """Deterministic multi-site ranking/optimization under constraints — the
    normalization contract that lets an agent compare sites across separate
    analyze_site calls WITHOUT dropping into code (Grok's rails-thin gap). Body:
      {
        "candidates": [{"id":.., "lat":.., "lng":.., "<metric>":num, ..}, ...],
        "constraints": {"<field>": {"min":num, "max":num}, ...},   # hard filters
        "objectives":  {"<field>": weight, ...},   # +weight maximizes, -minimizes
        "top_k": 3
      }
    Each objective is min-max normalized 0-100 across the candidates that pass the
    constraints (100=best in that set), then combined as a |weight|-weighted mean."""
    body = request.get_json(silent=True) or {}
    cands = body.get("candidates") or []
    constraints = body.get("constraints") or {}
    objectives = body.get("objectives") or {}
    # shortlist_name (alias shortlist_id) → one-shot re-rank of a SAVED shortlist:
    # load its sites (owner-scoped) as candidates so an agent re-ranks a persistent
    # shortlist against the current baseline in a single call (Phase 5 ergonomics).
    _shortlist = (body.get("shortlist_name") or body.get("shortlist_id") or "").strip()
    _from_shortlist = None
    if _shortlist and not cands:
        try:
            import hashlib
            _key = (request.headers.get("X-API-Key", "") or "").strip()
            _own = ("k_" + hashlib.sha256(_key.encode()).hexdigest()[:24]) if _key else "public"
            with psycopg.connect(NEON_URL, autocommit=True) as _sc:
                _cur = _sc.cursor()
                _cur.execute("SELECT site_ref, lat, lng, capacity_mw, saved_metrics, saved_objectives "
                             "FROM agent_shortlist_sites WHERE owner=%s AND shortlist_name=%s",
                             (_own, _shortlist))
                _rows = _cur.fetchall()
            _saved_obj = {}
            for _sr, _la, _ln, _cap, _mx, _ob in _rows:
                row = dict(_mx) if isinstance(_mx, dict) else {}
                row.update({"site_ref": _sr, "lat": (float(_la) if _la is not None else None),
                            "lng": (float(_ln) if _ln is not None else None),
                            "capacity_mw": (float(_cap) if _cap is not None else None)})
                cands.append(row)
                if isinstance(_ob, dict) and not _saved_obj:
                    _saved_obj = _ob
            # if the caller didn't pass objectives, reuse the ones the shortlist was saved under
            if not objectives and _saved_obj:
                objectives = _saved_obj
            _from_shortlist = {"shortlist_name": _shortlist, "sites_loaded": len(cands)}
        except Exception as _e:
            return jsonify(ok=False, _entity="error", error=f"shortlist load failed: {str(_e)[:120]}"), 200
    # absolute=true → score each objective on a FIXED 0-100 scale (the raw value,
    # clamped, direction-adjusted) instead of min-max within the batch. Gives
    # cross-run-stable scores (Grok's relative-vs-absolute gap) — but ONLY valid
    # when the objective fields are already 0-100 (e.g. analyze_site's scores:
    # risk_resilience, fiber_connectivity, water score). Pass 0-100 score fields,
    # NOT raw distances like fiber_km, in absolute mode.
    absolute = str(body.get("absolute", "")).lower() in ("1", "true", "yes")
    # percentile=true → score each objective as its PERCENTILE against the viable-site
    # population (the reference distribution maintained by the site-baseline master
    # tick). Answers "better than X% of viable sites", stable across runs AND
    # comparable across regions — the global-comparability layer Gemini+Grok flagged.
    # Takes precedence over absolute. Fields with no baseline fall back to absolute.
    percentile = str(body.get("percentile", "")).lower() in ("1", "true", "yes")
    _baseline = {}
    _unbaselined = []
    if percentile:
        try:
            from site_baseline import load_baseline, percentile_of as _pct_of
            _baseline = load_baseline(force=True)
        except Exception:
            _baseline = {}
    try:
        top_k = max(1, int(body.get("top_k") or 3))
    except Exception:
        top_k = 3
    # r-require-complete (2026-07-12, Grok's Option A from its unhappy-path
    # audit): declared missing_objectives is necessary but NOT sufficient — an
    # incomplete candidate could win rank 1 on its single best metric while
    # naive agents just take rank 1. require_complete=true (opt-in, default
    # false preserves current behavior) DROPS candidates missing any VALIDATED
    # objective and DECLARES them in excluded_incomplete — control stays with
    # the agent, fail-closed house style. Completeness is measured against
    # validated objectives only (a set-wide-unavailable objective can't
    # disqualify everyone).
    require_complete = str(body.get("require_complete", "")).lower() in ("1", "true", "yes")
    if not isinstance(cands, list) or not cands:
        return jsonify(ok=False, _entity="error", error="candidates must be a non-empty list"), 400
    if not isinstance(objectives, dict) or not objectives:
        return jsonify(ok=False, _entity="error",
                       error="objectives required: {field: weight} — +weight maximizes, -weight minimizes"), 400

    def _num(v):
        try:
            return float(v)
        except Exception:
            return None

    # 1. hard constraints (fail-closed: a candidate missing a constrained field is dropped)
    def _passes(c):
        for f, rule in (constraints or {}).items():
            v = _num(c.get(f))
            if v is None:
                return False
            if isinstance(rule, dict):
                if "min" in rule and rule["min"] is not None and v < float(rule["min"]):
                    return False
                if "max" in rule and rule["max"] is not None and v > float(rule["max"]):
                    return False
        return True

    # r-eval-fixwave (2026-07-11): accept the {"id", "fields": {...}} candidate
    # shape — two independent platform evaluators (Mistral Large, Llama 4 Scout)
    # and our own showcase demo all reached for a nested fields wrapper, and it
    # silently no-op'd every objective (coverage honestly said "0 evaluated" but
    # ranks came back as input order with 0.0 scores). Postel: flatten fields
    # into the candidate; explicit top-level keys win on collision.
    def _flat(c):
        if isinstance(c.get("fields"), dict):
            merged = dict(c["fields"])
            merged.update({k: v for k, v in c.items() if k != "fields"})
            return merged
        return dict(c)

    cands = [_flat(c) for c in cands if isinstance(c, dict)]

    # r-candidate-v1 (2026-07-11, ChatGPT co-design): accept {"candidate_id":
    # "cand_…"} entries. Frozen identity fields (lat/lng/capacity_mw/fiber_km/
    # iso) come from the mint — never reinterpreted, agent metrics overlay the
    # rest. Expired/unknown ids are DROPPED AND DECLARED (fail-closed, the
    # deterministic-expiry contract) — never silently re-resolved.
    # audit-fix 2026-07-12: expired/unknown/READ-FAILED candidate_id entries are
    # all DECLARED (fail-closed) — a transient read fault must NOT silently let
    # an entry rank on its raw caller fields (that would reinterpret a candidate
    # the mint should have frozen). read_failed is distinct from unknown so an
    # agent can retry vs. re-search.
    _expired_cands, _unknown_cands, _readfailed_cands = [], [], []
    if any(c.get("candidate_id") for c in cands):
        from routes.candidates import load_candidate, CandidateReadUnavailable
        _resolved, _cc = [], None
        try:
            _cc = psycopg.connect(NEON_URL, autocommit=True)
            _ccur = _cc.cursor()
        except Exception:
            _ccur = None
        for c in cands:
            cid = (c.get("candidate_id") or "").strip()
            if not cid:
                _resolved.append(c)
                continue
            if _ccur is None:  # connection down → declare, never reinterpret
                _readfailed_cands.append(cid)
                continue
            try:
                cand, _exp = load_candidate(_ccur, cid)
            except CandidateReadUnavailable:
                _readfailed_cands.append(cid)
                continue
            if cand is None:
                _unknown_cands.append(cid)
                continue
            if _exp:
                _expired_cands.append(cid)
                continue
            frozen = {k: v for k, v in {
                "id": c.get("id") or cand.get("project_name") or cid,
                "candidate_id": cid, "lat": cand.get("lat"),
                "lng": cand.get("lng"), "capacity_mw": cand.get("capacity_mw"),
                "fiber_km": cand.get("fiber_km"), "iso": cand.get("iso"),
            }.items() if v is not None}
            _resolved.append({**c, **frozen})
        if _cc is not None:
            try: _cc.close()
            except Exception: pass
        cands = _resolved

    survivors = [dict(c) for c in cands if _passes(c)]
    dropped = len(cands) - len(survivors)

    # 2. score each objective 0-100. absolute mode = fixed 0-100 scale (stable across
    #    runs); relative mode (default) = min-max within THIS batch (100 = best in set).
    norm_basis = {}
    coverage = {}          # {objective: validated | unavailable}  (ChatGPT's Constraint Coverage)
    objective_status = []  # [{objective, status, basis|reason}] — declare, never silently skip
    unsupported = _effective_unsupported()  # water drops out once real WRI rows land
    for f, w in objectives.items():
        w = _num(w) or 0.0
        vals = [_num(c.get(f)) for c in survivors]
        vals = [v for v in vals if v is not None]
        # ── Constraint coverage: an unsupported/absent objective is DECLARED, not
        #    dropped, and is excluded from the weighted score so it can't dilute or
        #    fabricate the ranking. The recommendation is "conditionally complete".
        if f in unsupported:
            coverage[f] = "unavailable"
            objective_status.append({"objective": f, "status": "unavailable",
                                     "reason": unsupported[f]})
            continue
        if not vals:
            coverage[f] = "unavailable"
            objective_status.append({"objective": f, "status": "unavailable",
                                     "reason": "no candidate carried a numeric value for this objective"})
            continue
        coverage[f] = "validated"
        # r-eval-fixwave-3 (2026-07-11): the percentile→absolute fallback
        # CLAMPED raw-scale fields (capacity_mw 960/1310/1400 → all 100.0,
        # ttp 24 → 76.0 for everyone) — zero discriminative signal while
        # objective_status still said "validated" (GPT-5.5 finding, confirmed
        # again on the Llama re-run harness). Unbaselined fields under
        # percentile now fall back to RELATIVE (min-max within this batch),
        # which discriminates at any scale; the explicit absolute=true mode
        # keeps its documented fixed-scale contract for 0-100 fields.
        if percentile and f in _baseline:
            objective_status.append({"objective": f, "status": "validated", "basis": "percentile_vs_population"})
        elif percentile and f not in _baseline:
            if f not in _unbaselined:
                _unbaselined.append(f)
            objective_status.append({"objective": f, "status": "validated",
                                     "basis": "relative_fallback_not_in_baseline"})
        elif absolute:
            objective_status.append({"objective": f, "status": "validated", "basis": "absolute_0_100"})
        else:
            objective_status.append({"objective": f, "status": "validated", "basis": "relative_in_set"})
        if percentile and f in _baseline:
            bmeta = _baseline[f]
            norm_basis[f] = {"scale": "percentile vs population", "p50": bmeta.get("p50"),
                             "sample_size_metric": None, "direction": "maximize" if w >= 0 else "minimize"}
            for c in survivors:
                pv = _pct_of(f, _num(c.get(f)), _baseline)
                if pv is None:
                    continue
                c.setdefault("_norm", {})[f] = round(pv if w >= 0 else (100.0 - pv), 1)
        elif absolute and not percentile:
            norm_basis[f] = {"scale": "0-100 fixed", "direction": "maximize" if w >= 0 else "minimize"}
            for c in survivors:
                v = _num(c.get(f))
                if v is None:
                    continue
                v = max(0.0, min(100.0, v))
                c.setdefault("_norm", {})[f] = round(v if w >= 0 else (100.0 - v), 1)
        else:
            lo, hi = min(vals), max(vals)
            span = (hi - lo) or 1.0
            norm_basis[f] = {"min": round(lo, 3), "max": round(hi, 3),
                             "direction": "maximize" if w >= 0 else "minimize"}
            for c in survivors:
                v = _num(c.get(f))
                if v is None:
                    continue
                n = (v - lo) / span * 100.0
                c.setdefault("_norm", {})[f] = round(n if w >= 0 else (100.0 - n), 1)

    # 3. weighted objective score + rank — over VALIDATED objectives only, so an
    #    unavailable objective neither dilutes the score nor gets silently zero-filled.
    # r-eval-fixwave (2026-07-11): PER-CANDIDATE renormalization. A candidate
    # missing a set-validated objective used to have it scored as 0 — silent
    # dilution (Mistral: "rank poorly even if they might score well"). Now the
    # weighted mean runs over the objectives the candidate actually carries,
    # the gap is DECLARED in missing_objectives, and a candidate carrying none
    # of them scores null (ranked last), never a fabricated 0.0.
    _validated = [f for f in objectives if coverage.get(f) == "validated"]
    for c in survivors:
        nrm = c.get("_norm", {})
        present = [f for f in _validated if f in nrm]
        missing = [f for f in _validated if f not in nrm]
        cw = sum(abs(_num(objectives[f]) or 0.0) for f in present)
        if present and cw:
            s = sum((abs(_num(objectives[f]) or 0.0)) * nrm[f] for f in present)
            c["objective_score"] = round(s / cw, 1)
        else:
            c["objective_score"] = None
        if missing:
            c["missing_objectives"] = missing
        c["normalized"] = c.pop("_norm", {})
    # r-require-complete: opt-in drop of incomplete candidates — declared,
    # never silent (Grok: "declaring the missing fields is necessary but not
    # sufficient" when rank 1 is all a naive agent reads).
    _excluded_incomplete = []
    if require_complete:
        _keep = []
        for c in survivors:
            if c.get("missing_objectives") or c["objective_score"] is None:
                _excluded_incomplete.append({
                    "id": c.get("id") or c.get("project_name"),
                    **({"candidate_id": c["candidate_id"]} if c.get("candidate_id") else {}),
                    "missing_objectives": c.get("missing_objectives")
                        or [f for f in _validated],
                })
            else:
                _keep.append(c)
        survivors = _keep
    survivors.sort(key=lambda c: (c["objective_score"] is not None,
                                  c["objective_score"] or 0.0), reverse=True)
    for i, c in enumerate(survivors):
        c["rank"] = i + 1

    _mode = "percentile" if percentile else ("absolute" if absolute else "relative")
    _extra = {}
    _caveats = []
    if _from_shortlist:
        _extra["from_shortlist"] = _from_shortlist
    if percentile:
        _extra["percentile_baseline_available"] = sorted(_baseline.keys())
        if _unbaselined:
            _extra["unbaselined_fields_fell_back_to_relative"] = _unbaselined
        # Phase 4: baseline freshness so an agent can reason about reproducibility.
        try:
            from site_baseline import baseline_meta
            _extra["baseline"] = baseline_meta()
            _bage = (_extra["baseline"] or {}).get("age_hours")
            if _bage is not None and _bage > 48:
                _caveats.append(f"reference baseline is {round(_bage)}h old — trigger /api/jobs/site-baseline to refresh")
        except Exception:
            pass
    # Phase 4: small-candidate-set caveat — percentiles are vs the national
    # population, so a small submitted set can look poor even if it's the best
    # available; the agent should read percentiles as population rank, not set rank.
    if percentile and len(survivors) < 8:
        _caveats.append(f"only {len(survivors)} candidate(s) scored — percentile is rank vs the full viable population, NOT vs these {len(survivors)}; a low score means 'below the national median', not 'worst of your set'")
    # Constraint Coverage — declare which objectives were actually evaluated, so an
    # agent (or human) sees a "conditionally complete" answer instead of a silently
    # partial one. An unavailable objective is named + reasoned, never approximated.
    _extra["constraint_coverage"] = coverage
    # ★2026-08-25: one name, four shapes across the fleet — say which one
    # this is so an agent can branch instead of sniffing types.
    _extra["constraint_coverage_shape"] = _cc_shape(coverage)
    _extra["objective_status"] = objective_status
    if require_complete:
        _extra["require_complete"] = True
        _extra["excluded_incomplete"] = _excluded_incomplete
    # r-candidate-v1: the declared fail-closed outcomes for candidate_id entries,
    # + the contract echo so an auditor knows which implementations touched this.
    if (_expired_cands or _unknown_cands or _readfailed_cands
            or any(c.get("candidate_id") for c in survivors)):
        _cc_block = {
            "search_version": "refined-queue/2026-07-11",
            "analysis_version": "rank-sites/2026-07-11",
            "expired_candidates": _expired_cands,
            "unknown_candidates": _unknown_cands,
        }
        # read_failed = transient store fault; declared so an agent RETRIES
        # these rather than treating them as unknown/re-search (audit-fix).
        if _readfailed_cands:
            _cc_block["read_failed_candidates"] = _readfailed_cands
            _cc_block["read_failed_note"] = ("candidate store temporarily "
                "unavailable for these ids — retry; they were NOT ranked and "
                "NOT reinterpreted on caller fields")
        _extra["candidate_contract"] = _cc_block
    _unavail = [f for f in objectives if coverage.get(f) == "unavailable"]
    if _unavail:
        _extra["coverage_note"] = (
            f"{len(_validated)} of {len(objectives)} objectives evaluated; ranking reflects ONLY "
            f"validated objectives. Unavailable ({', '.join(_unavail)}) were excluded from the weighted "
            f"score — not fabricated, not zero-filled. Treat the result as conditionally complete.")
    if _caveats:
        _extra["caveats"] = _caveats
    return jsonify({
        "_entity": "ranked_sites",
        "ok": True,
        "scoring_mode": _mode,
        **_extra,
        "count_ranked": len(survivors),
        "count_returned": min(top_k, len(survivors)),
        "dropped_by_constraints": dropped,
        "results": survivors[:top_k],
        "normalization_basis": norm_basis,
        "_source": "DC Hub — dchub.cloud",
        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"",
        "note": ("Deterministic ranking. constraints are HARD filters (fail-closed on a missing "
                 "constrained field). Each objective is min-max normalized to 0-100 ACROSS the "
                 "candidates that passed constraints (100=best in THIS set), then combined as a "
                 "|weight|-weighted mean -> objective_score. objectives: +weight maximizes, "
                 "-weight minimizes. Each result carries normalized{} (the per-field 0-100 you can "
                 "cite) + rank + objective_score; normalization_basis gives the min/max/direction "
                 "used. DEFAULT (relative) scores are relative to the submitted set — re-rank the SAME "
                 "candidate set for stable comparisons. Pass absolute=true for CROSS-RUN-STABLE scores "
                 "on a fixed 0-100 scale (use only when the objective fields are already 0-100, e.g. "
                 "analyze_site's risk_resilience / fiber_connectivity scores — not raw distances like "
                 "fiber_km). scoring_mode echoes which was used. Pass the site_evaluation_handoff through "
                 "on each candidate so the rail stays unbroken into the winners."),
    })


@interconnection_queues_bp.route("/api/v1/interconnection-queue/refined")
# r-slow-tool-cache (2026-08-31): p50 10,991 ms / p95 12,854 ms over 65 calls in
# the 14 days to 2026-08-31 — a MEDIAN past most MCP client timeouts. The
# response is a pure function of the filter args below (a queue slice, not a
# per-caller view), and the underlying ISO queue tables refresh daily at most,
# so a 1 h memo costs nothing in freshness. EVERY arg the handler reads is named
# in arg_names — an unnamed arg would be excluded from the key and served a
# stale answer. See routes/_slow_tool_cache.py for why cached_endpoint does not
# fit (it skips authenticated requests).
@cache_tool_response(
    ttl=3600, prefix='refined_queue',
    arg_names=('min_mw', 'max_ttp_months', 'iso', 'baseload_only', 'fuel_type',
               'status', 'max_fiber_km', 'geocoded_only', 'limit'))
def api_refined_queue():
    import re as _re
    a = request.args
    def _num(k, d=None):
        v = a.get(k)
        try:
            return float(v) if v not in (None, "") else d
        except Exception:
            return d
    min_mw = _num("min_mw", 0) or 0
    max_ttp = _num("max_ttp_months", None)
    iso = (a.get("iso") or "").strip()
    baseload_only = str(a.get("baseload_only", "")).lower() in ("1", "true", "yes")
    fuel_type = (a.get("fuel_type") or "").strip()
    status = (a.get("status") or "active").strip().lower()
    max_fiber_km = _num("max_fiber_km", None)
    geocoded_only = str(a.get("geocoded_only", "")).lower() in ("1", "true", "yes")
    try:
        # 2026-07-18: ceiling 1000 -> 5000 so a single per-ISO pull returns the
        # WHOLE active queue for the largest ISOs (ERCOT ~1,785 active geocoded was
        # capped at 1,000, silently dropping ~785). Ordered by capacity DESC, so the
        # old 1,000 kept the biggest; the map now unions the full set. Public ISO
        # queue data; 5k rows is a bounded seq-scan on the ~5k-row table.
        limit = max(1, min(int(a.get("limit") or 200), 5000))
    except Exception:
        limit = 200

    where = ["capacity_mw IS NOT NULL", "capacity_mw >= %s"]
    params = [min_mw]
    if iso:
        # Comma/semicolon-separated ISO union; normalize away non-alnum so ISO-NE,
        # ISONE, iso-ne all collapse to ISONE and match the stored label. e.g.
        # iso=ERCOT,PJM keeps both; combines with max_ttp_months as an intersection.
        _iso_toks = [_re.sub(r"[^A-Z0-9]", "", t.strip().upper())
                     for t in iso.replace(";", ",").split(",") if t.strip()]
        _iso_toks = [t for t in _iso_toks if t]
        if _iso_toks:
            where.append("regexp_replace(upper(coalesce(iso,'')),'[^A-Z0-9]','','g') = ANY(%s)")
            params.append(_iso_toks)
    if status == "all":
        pass  # no status filter
    elif status == "active":
        # in-progress = not terminal; cross-ISO safe (SPP labels lack the word "active")
        where.append("coalesce(queue_status,'') !~* %s"); params.append(_DEAD_STATUS_RE)
    elif status:
        where.append("lower(coalesce(queue_status,'')) LIKE %s"); params.append("%" + status + "%")
    if baseload_only:
        where.append("coalesce(fuel_type,'') !~* %s"); params.append(_NON_BASELOAD_RE)
    if fuel_type:
        # Inclusive substring match on the raw fuel label (POSIX ~*): comma/semicolon
        # separated tokens, keep rows matching ANY. e.g. "gas" hits GAS/Natural Gas/
        # "Natural Gas; Other"; "nuclear,hydro" unions the two. Alnum/space/+/- only.
        _toks = [t.strip() for t in fuel_type.replace(";", ",").split(",") if t.strip()]
        _toks = [t for t in _toks if _re.fullmatch(r"[A-Za-z0-9 /+-]{1,24}", t)]
        if _toks:
            where.append("coalesce(fuel_type,'') ~* %s")
            params.append("(" + "|".join(_toks) + ")")
    # r-eval-fixwave-3 (2026-07-11): make the TTP hard cut SELF-EXPLAINING.
    # Two independent evaluators (GPT-5.5, Llama 4) hit a zero-result set from
    # max_ttp_months below the ISO averages and read it as missing data / a
    # geocode bug — one burned half its call budget on structurally-empty
    # queries. The zero was CORRECT but not legible at the moment it happened.
    # Now the response always carries isos_excluded_by_ttp + the minimum
    # satisfiable threshold for the requested scope.
    _ttp_excluded = {}
    _ttp_min_satisfiable = None
    if max_ttp is not None:
        _requested = ([t for t in _iso_toks] if (iso and _iso_toks) else
                      [_re.sub(r"[^A-Z0-9]", "", k.upper()) for k in _ISO_TTP_MONTHS])
        _ttp_by_norm = {_re.sub(r"[^A-Z0-9]", "", k.upper()): v
                        for k, v in _ISO_TTP_MONTHS.items()}
        _ttp_excluded = {k: _ttp_by_norm[k] for k in _requested
                         if k in _ttp_by_norm and _ttp_by_norm[k] > max_ttp}
        _in_scope = [_ttp_by_norm[k] for k in _requested if k in _ttp_by_norm]
        _ttp_min_satisfiable = min(_in_scope) if _in_scope else None
        ttp_isos = [k.upper() for k, v in _ISO_TTP_MONTHS.items() if v <= max_ttp]
        if ttp_isos:
            where.append("upper(iso) = ANY(%s)"); params.append(ttp_isos)
        else:
            where.append("1=0")
    # Phase-2 spatial predicates (only geocoded rows qualify; 83% of the queue).
    if geocoded_only or max_fiber_km is not None:
        where.append("lat IS NOT NULL AND lng IS NOT NULL")
    if max_fiber_km is not None:
        # fiber_km = km to nearest MAPPED long-haul route endpoint (coarse backbone
        # proximity, sparse ~260-node dataset; origin is a county-centroid for most rows).
        where.append("fiber_km IS NOT NULL AND fiber_km <= %s"); params.append(max_fiber_km)

    if not NEON_URL:
        return jsonify(ok=False, error="no_db", _entity="error"), 503
    wc = " AND ".join(where)
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT project_name, iso, state, county, fuel_type, capacity_mw, "
                "queue_status, queue_date, queue_id, poi_name, lat, lng, fiber_km, geo_method "
                "FROM interconnect_queue "
                "WHERE " + wc + " ORDER BY capacity_mw DESC NULLS LAST LIMIT %s",
                params + [limit])
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*), COALESCE(SUM(capacity_mw),0) "
                        "FROM interconnect_queue WHERE " + wc, params)
            total_n, total_mw = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], _entity="error"), 200

    by_iso, by_fuel = {}, {}
    survivors = []
    n_geocoded = 0
    for d in rows:
        iso_u = (d.get("iso") or "").upper()
        d["capacity_mw"] = float(d["capacity_mw"]) if d["capacity_mw"] is not None else None
        d["queue_date"] = d["queue_date"].isoformat() if d.get("queue_date") else None
        d["estimated_ttp_months"] = _ISO_TTP_MONTHS.get(iso_u) or _ISO_TTP_MONTHS.get(iso_u.replace("-", ""))
        firm = not _re.search(_NON_BASELOAD_RE.replace(r"\y", r"\b"), d.get("fuel_type") or "", _re.I)
        d["fuel_class"] = "firm/baseload-capable" if firm else "intermittent"
        # provenance-v1 (2026-07-11): every interconnect_queue row is a
        # published ISO queue entry (project/MW/status straight from the ISO's
        # public queue). Inferred ENRICHMENTS stay flagged by their own
        # fields: coordinate_precision (county_centroid = inferred locus) and
        # estimated_ttp_months (ISO-average, not per-project).
        d["v"] = "published"
        # Phase-2 geo enrichment: normalize coords + fiber_km, attach a compact
        # ready-to-pipe site_evaluation_handoff (verbose 'why' stays in the top-level
        # handoff / note, so per-survivor payload stays lean).
        lat = float(d["lat"]) if d.get("lat") is not None else None
        lng = float(d["lng"]) if d.get("lng") is not None else None
        d["lat"], d["lng"] = lat, lng
        d["fiber_km"] = float(d["fiber_km"]) if d.get("fiber_km") is not None else None
        d["coordinate_precision"] = d.pop("geo_method", None)  # poi_exact | county_centroid
        if lat is not None and lng is not None:
            n_geocoded += 1
            mw = d["capacity_mw"]
            # representative_point = the invariant contract anchor an agent keys on.
            # Phase 3 will add an optional `geometry` passenger ALONGSIDE it (the
            # point never changes shape); for a future MultiPolygon this is the
            # centroid of the largest-area member, never the multi-part center.
            d["representative_point"] = {"lat": lat, "lng": lng}
            d["site_evaluation_handoff"] = {
                "analyze_site": ({"lat": lat, "lon": lng, "capacity_mw": mw, "include_risk": True, "include_fiber": True}
                                 if mw else {"lat": lat, "lon": lng, "include_risk": True, "include_fiber": True}),
                "get_water_risk": {"lat": lat, "lng": lng},
            }
        else:
            d["representative_point"] = None
            d["site_evaluation_handoff"] = None
        by_iso[iso_u] = by_iso.get(iso_u, 0) + 1
        by_fuel[d["fuel_class"]] = by_fuel.get(d["fuel_class"], 0) + 1
        survivors.append(d)

    # r-candidate-v1 (2026-07-11, ChatGPT co-design): mint a durable, opaque
    # candidate_id per survivor — downstream tools consume the id instead of
    # transposing coordinates/filters (the transcription-drift class). Fail-
    # soft: a mint failure must never break the search response itself.
    _snapshot_id = None
    _SV = _TTL = None
    try:
        from routes.candidates import mint_candidates, SEARCH_VERSION as _SV, TTL_DAYS as _TTL
        with psycopg.connect(NEON_URL, autocommit=True) as _mc:
            _mcur = _mc.cursor()
            _mcur.execute("SELECT 'snap_' || MAX(loaded_at)::date FROM interconnect_queue")
            _snapshot_id = (_mcur.fetchone() or [None])[0] or None
            if _snapshot_id:
                mint_candidates(_mcur, survivors, _snapshot_id,
                                {"min_mw": min_mw, "max_ttp_months": max_ttp,
                                 "iso": iso or None, "baseload_only": baseload_only,
                                 "fuel_type": fuel_type or None, "status": status,
                                 "max_fiber_km": max_fiber_km,
                                 "geocoded_only": geocoded_only})
    except Exception:
        _snapshot_id = None  # search still serves; candidates just absent this response

    _resp = {
        "_entity": "queue_results",
        "ok": True,
        **({"snapshot_id": _snapshot_id, "search_version": _SV,
            "candidate_ttl_days": _TTL} if _snapshot_id else {}),
        "count_returned": len(survivors),
        "count_total_matching": int(total_n or 0),
        "total_queued_mw": round(float(total_mw or 0), 1),
        "filters_applied": {"min_mw": min_mw, "max_ttp_months": max_ttp,
                            "iso": iso or None, "baseload_only": baseload_only,
                            "fuel_type": fuel_type or None, "status": status,
                            "max_fiber_km": max_fiber_km, "geocoded_only": geocoded_only},
        # r-eval-fixwave-3: the TTP hard cut explains itself — which requested
        # ISOs it eliminated (with their averages) and the smallest
        # max_ttp_months that would return anything for this scope.
        **({"isos_excluded_by_ttp": _ttp_excluded,
            "min_satisfiable_max_ttp_months": _ttp_min_satisfiable}
           if max_ttp is not None else {}),
        "results": survivors,
        "summary": {"by_iso": by_iso, "by_fuel_class": by_fuel,
                    "geocoded_returned": n_geocoded},
        "_source": "DC Hub — dchub.cloud",
        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"",
        # provenance-v1 (2026-07-11): collection block (additive — the legacy
        # _source/_cite strings above stay for back-compat). Per-record v on
        # each result: published = the ISO's own queue row; coordinate_precision
        # + estimated_ttp_months flag the inferred enrichments per row.
        # v1 default_v=inferred: refined rows carry DC Hub enrichments
        # (county-centroid geocoding, ISO-average TTP), so an unflagged
        # record conservatively inherits inferred.
        "provenance": _snapshot_provenance(default_v="inferred"),
        "note": ("Server-side set-reduction over the live ISO interconnection queue "
                 "(~5,300 projects, 7 ISOs). fuel_type is an inclusive substring match on "
                 "the raw fuel label (e.g. 'gas' hits GAS/Natural Gas); baseload_only excludes "
                 "wind/solar/storage; iso accepts a comma-separated union. status=active (default) "
                 "means still-progressing — it excludes terminal states (withdrawn/cancelled/"
                 "suspended/in-commercial-operation) rather than matching the literal word, so it "
                 "is cross-ISO safe (SPP labels its live projects 'IA FULLY EXECUTED/ON SCHEDULE', "
                 "'DISIS STAGE', etc.). PHASE 2 LIVE: ~83% of rows now carry a representative_point "
                 "{lat,lng} (the invariant anchor — Phase 3 adds an optional GeoJSON `geometry` "
                 "passenger alongside it) + a compact per-survivor site_evaluation_handoff (ready-to-pipe "
                 "analyze_site + get_water_risk args). coordinate_precision is 'poi_exact' (matched a named substation) or "
                 "'county_centroid' (county-median of substation locations, ~county resolution — not "
                 "parcel-exact). fiber_km + max_fiber_km = km to the nearest MAPPED long-haul route "
                 "endpoint (coarse backbone-proximity from a sparse ~260-node dataset, NOT last-mile "
                 "fiber). Use geocoded_only=true to keep only survivors an agent can pipe into "
                 "analyze_site. estimated_ttp_months stays the ISO-level average (DCPI); a per-project "
                 "ETA is the remaining refinement."),
    }
    # _error_mitigation (2026-07-12, Gemini error_version:1): the TTP zero-cut is
    # the reference in-band self-correction rail. When the max_ttp_months filter
    # provably eliminated everything, attach a deterministic path back — severity
    # parameter_adjustment, suggested_params.max_ttp_months = the COMPUTED
    # min-satisfiable value (never a static guess). The MCP layer flips
    # isError:true on presence so the agent acts on it instead of reading an
    # empty success. Registry/templating in routes/error_mitigation.py.
    if (max_ttp is not None and int(total_n or 0) == 0
            and _ttp_excluded and _ttp_min_satisfiable is not None):
        try:
            from routes.error_mitigation import build_mitigation
            _mit = build_mitigation(
                "zero_row_ttp_cut",
                computed={"min_satisfiable": _ttp_min_satisfiable},
                suggested={"max_ttp_months": _ttp_min_satisfiable})
            if _mit:
                _resp["_error_mitigation"] = _mit
        except Exception:
            pass
    return jsonify(_resp)


@interconnection_queues_bp.route("/api/v1/interconnection-queue/changes")
def api_queue_changes():
    """WS6 (2026-07-28) — interconnection-queue transitions, from the day capture
    started forward. Served from interconnect_queue_events, written at INGEST
    time by /api/v1/iso-queue/ingest-projects — never inferred at read time,
    because interconnect_queue carries no withdrawal signal to infer from: the
    loader is a pure UPSERT with no DELETE (load_interconnect_queue_live.py:
    602-619) and 5 of the 7 parsers write a hardcoded queue_status='active', so a
    departed project keeps a row reading 'active' indefinitely.

    HONEST-NUMBERS CONTRACT — three parts, all enforced below:
      1. `disappeared_from_feed` is a PROXY for withdrawal, not a withdrawal. No
         ISO feed in this pipeline ever says "withdrawn"; the project simply
         stops appearing. It is published under its own name, never as a
         withdrawal count or a withdrawal rate.
      2. `history_starts_at` rides on every response. Zero disappearances in a
         window means "we have N days of history", never "nothing left the
         queue".
      3. Coverage is counted in DAYS, and any ISO with a day that produced no
         parse_ok run — or no run at all — returns
         disappeared_from_feed_count = null plus a coverage_gaps entry.
         UNMEASURED is null, never 0. A feed outage must never read as a
         withdrawal event.

    Params: iso (comma union), since_days (1-400, default 30), event_type,
    min_mw, limit (1-2000, default 200), include_seed.
    """
    import re as _re
    a = request.args

    def _clamp(k, d, lo, hi):
        v = a.get(k)
        if v in (None, ""):
            return d
        try:
            return max(lo, min(int(v), hi))
        except Exception:
            return d

    since_days = _clamp("since_days", 30, 1, 400)
    limit = _clamp("limit", 200, 1, 2000)
    iso = (a.get("iso") or "").strip()
    event_type = (a.get("event_type") or "").strip().lower()
    include_seed = str(a.get("include_seed", "")).lower() in ("1", "true", "yes")
    try:
        min_mw = float(a.get("min_mw")) if a.get("min_mw") not in (None, "") else None
    except Exception:
        min_mw = None
    _TYPES = ("appeared_in_feed", "capacity_changed", "status_changed",
              "disappeared_from_feed", "seed")
    if event_type and event_type not in _TYPES:
        return jsonify(ok=False, error="bad_event_type", allowed=list(_TYPES),
                       _entity="error"), 400
    # same ISO normalization as /refined so ISO-NE / ISONE / iso-ne all collapse
    _iso_toks = [_re.sub(r"[^A-Z0-9]", "", t.strip().upper())
                 for t in iso.replace(";", ",").split(",") if t.strip()]
    _iso_toks = [t for t in _iso_toks if t]
    _NORM = "regexp_replace(upper(coalesce({}.iso,'')),'[^A-Z0-9]','','g') = ANY(%s)"

    if not NEON_URL:
        return jsonify(ok=False, error="no_db", _entity="error"), 503

    _NOTE = ("disappeared_from_feed is a PROXY for withdrawal, NOT a withdrawal — no "
             "ISO feed here ever says 'withdrawn', the project just stops appearing, "
             "so treat it as an exit signal to confirm, not a confirmed exit. Read "
             "every count against history_starts_at: before capture existed the loader "
             "was a pure UPSERT with no DELETE, so a departed project kept a row "
             "reading queue_status='active' indefinitely and no per-project history was "
             "retained at all. A null count is UNMEASURED for that ISO in that window "
             "(see coverage_gaps), never zero. rows_in_table_not_in_last_good_feed is "
             "the accumulated ghost backlog still counted as live capacity by every "
             "capacity_mw aggregate, including this table's own /refined endpoint.")

    ev_where = ["e.detected_at >= NOW() - make_interval(days => %s)"]
    ev_params = [since_days]
    if _iso_toks:
        ev_where.append(_NORM.format("e")); ev_params.append(_iso_toks)
    if event_type:
        ev_where.append("e.event_type = %s"); ev_params.append(event_type)
    elif not include_seed:
        ev_where.append("e.event_type <> 'seed'")
    if min_mw is not None:
        ev_where.append("GREATEST(COALESCE(e.new_capacity_mw,0), "
                        "COALESCE(e.prev_capacity_mw,0)) >= %s")
        ev_params.append(min_mw)
    ev_wc = " AND ".join(ev_where)

    # the disappearance count runs on its OWN filter set (never narrowed by
    # event_type) so the headline exit signal is always present with its coverage
    d_where = ["e.detected_at >= NOW() - make_interval(days => %s)",
               "e.event_type = 'disappeared_from_feed'"]
    d_params = [since_days]
    if _iso_toks:
        d_where.append(_NORM.format("e")); d_params.append(_iso_toks)
    if min_mw is not None:
        d_where.append("COALESCE(e.prev_capacity_mw,0) >= %s"); d_params.append(min_mw)
    d_wc = " AND ".join(d_where)

    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.interconnect_queue_events'), "
                        "to_regclass('public.interconnect_queue_runs')")
            _reg = cur.fetchone() or (None, None)
            if not (_reg[0] and _reg[1]):
                # capture has never run — say so, and emit null, not zero
                return jsonify({
                    "ok": True, "_entity": "queue_change_events",
                    "capture_status": "not_yet_capturing",
                    "history_starts_at": None,
                    "window_days": since_days, "window_fully_covered": False,
                    "counts": None, "disappeared_from_feed_count": None,
                    "coverage": {}, "coverage_gaps": [], "events": [],
                    "count_returned": 0,
                    "_source": "DC Hub — dchub.cloud",
                    "note": ("Capture has not run yet: the tables are created by the "
                             "first /api/v1/iso-queue/ingest-projects run after deploy. "
                             "Every count is null (UNMEASURED), not zero. " + _NOTE),
                })

            # per-ISO coverage counted in DAYS, not runs: the GH workflow retries a
            # Railway-blocked ISO on its own runner, so one failed attempt that the
            # fallback recovered is NOT a hole — a day with no parse_ok run is.
            c_params, c_iso = [since_days], ""
            if _iso_toks:
                c_iso = " AND " + _NORM.format("r"); c_params.append(_iso_toks)
            cur.execute(
                "SELECT r.iso, "
                "COUNT(DISTINCT (r.run_at AT TIME ZONE 'UTC')::date), "
                "COUNT(DISTINCT (r.run_at AT TIME ZONE 'UTC')::date) "
                "  FILTER (WHERE r.parse_ok IS TRUE), "
                "MIN(r.run_at) FILTER (WHERE r.parse_ok IS TRUE), "
                "MAX(r.run_at) FILTER (WHERE r.parse_ok IS TRUE) "
                "FROM interconnect_queue_runs r "
                "WHERE r.run_at >= NOW() - make_interval(days => %s)" + c_iso +
                " GROUP BY r.iso", c_params)
            win = {r[0]: r for r in cur.fetchall()}

            # all-time first run (the per-ISO history floor) + the ghost backlog
            # from that ISO's LATEST parse_ok run
            f_params, f_iso = [], ""
            if _iso_toks:
                f_iso = " WHERE " + _NORM.format("r"); f_params.append(_iso_toks)
            cur.execute(
                "SELECT r.iso, MIN(r.run_at), "
                "(SELECT r2.stale_not_in_feed FROM interconnect_queue_runs r2 "
                "  WHERE r2.iso = r.iso AND r2.parse_ok IS TRUE "
                "  ORDER BY r2.run_at DESC LIMIT 1) "
                "FROM interconnect_queue_runs r" + f_iso + " GROUP BY r.iso", f_params)
            allt = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            cur.execute("SELECT e.event_type, COUNT(*) FROM interconnect_queue_events e "
                        "WHERE " + ev_wc + " GROUP BY e.event_type", ev_params)
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}

            cur.execute("SELECT e.iso, COUNT(*) FROM interconnect_queue_events e "
                        "WHERE " + d_wc + " GROUP BY e.iso", d_params)
            d_raw = {r[0]: int(r[1]) for r in cur.fetchall()}

            cur.execute(
                "SELECT e.detected_at, e.iso, e.queue_id, e.event_type, e.project_name, "
                "e.prev_capacity_mw, e.new_capacity_mw, e.prev_status, e.new_status, "
                "e.prev_seen_at, e.run_id FROM interconnect_queue_events e WHERE " +
                ev_wc + " ORDER BY e.detected_at DESC, "
                "GREATEST(COALESCE(e.new_capacity_mw,0), COALESCE(e.prev_capacity_mw,0)) "
                "DESC LIMIT %s", ev_params + [limit])
            cols = [c.name for c in cur.description]
            events = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                for k in ("detected_at", "prev_seen_at"):
                    if d.get(k) is not None:
                        d[k] = d[k].isoformat()
                events.append(d)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], _entity="error"), 200

    # ── ★ REFUSE a disappearance count wherever coverage cannot carry it ──
    _today_ord = datetime.now(timezone.utc).date().toordinal()
    _win_start_ord = _today_ord - (since_days - 1)
    history_starts_at, coverage, gaps, disappeared = None, {}, [], {}
    for _iso, (_first_ever, _stale) in allt.items():
        if _first_ever is not None and (history_starts_at is None
                                        or _first_ever < history_starts_at):
            history_starts_at = _first_ever
        _w = win.get(_iso)
        _days = int(_w[1] or 0) if _w else 0
        _days_ok = int(_w[2] or 0) if _w else 0
        try:
            _expected = max(0, _today_ord - max(_win_start_ord,
                                                _first_ever.date().toordinal()) + 1)
        except Exception:
            _expected = None
        coverage[_iso] = {
            "days_with_runs": _days, "days_parse_ok": _days_ok,
            "days_expected": _expected,
            "first_parse_ok_at": _w[3].isoformat() if _w and _w[3] else None,
            "last_parse_ok_at": _w[4].isoformat() if _w and _w[4] else None,
            "history_starts_at": _first_ever.isoformat() if _first_ever else None,
            "rows_in_table_not_in_last_good_feed": (
                int(_stale) if _stale is not None else None),
        }
        _why = None
        if _days_ok == 0:
            _why = ("no capture run cleared the short-feed floor in this window — "
                    "UNMEASURED, not zero")
        elif _days_ok < _days:
            _why = ("{} of {} days had runs but none that cleared the short-feed "
                    "floor".format(_days - _days_ok, _days))
        elif _expected is not None and _days_ok < _expected:
            _why = ("capture produced a good run on {} of {} days in this window — "
                    "the missing days are a feed/cron outage, not an absence of "
                    "queue exits".format(_days_ok, _expected))
        if _why:
            gaps.append({"iso": _iso, "days_with_runs": _days,
                         "days_parse_ok": _days_ok, "days_expected": _expected,
                         "reason": _why})
            disappeared[_iso] = None
        else:
            disappeared[_iso] = int(d_raw.get(_iso, 0))
    for _iso in d_raw:
        if _iso not in disappeared:
            disappeared[_iso] = None
            gaps.append({"iso": _iso, "days_with_runs": 0, "days_parse_ok": 0,
                         "days_expected": None,
                         "reason": "events exist but no capture run is recorded — "
                                   "UNMEASURED"})
    for _tok in _iso_toks:
        if not any(_re.sub(r"[^A-Z0-9]", "", (k or "").upper()) == _tok
                   for k in coverage):
            disappeared[_tok] = None
            gaps.append({"iso": _tok, "days_with_runs": 0, "days_parse_ok": 0,
                         "days_expected": None,
                         "reason": "no capture run has ever been recorded for this ISO "
                                   "— UNMEASURED, not zero"})

    return jsonify({
        "ok": True, "_entity": "queue_change_events",
        "capture_status": "capturing" if coverage else "no_runs_recorded",
        "history_starts_at": (history_starts_at.isoformat()
                              if history_starts_at else None),
        "window_days": since_days,
        "window_fully_covered": bool(
            history_starts_at is not None and not gaps
            and history_starts_at.date().toordinal() <= _win_start_ord),
        "filters": {"iso": _iso_toks or None, "event_type": event_type or None,
                    "min_mw": min_mw, "include_seed": include_seed},
        "counts": counts,
        "disappeared_from_feed_count": disappeared,
        "coverage": coverage,
        "coverage_gaps": gaps,
        "events": events,
        "count_returned": len(events),
        "_source": "DC Hub — dchub.cloud",
        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"",
        "note": _NOTE,
    })
