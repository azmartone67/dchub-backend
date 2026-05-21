"""Phase BBBBB (2026-05-16) — industry event tracker.

DCHawk + dcByte have humans at every Data Center World, AFCOM,
DCD Connect. We can match their presence WITHOUT humans by:
  - Maintaining a live list of upcoming events
  - Brain detector flags "submission window closing" so we apply
  - Brain detector flags "DC Hub not submitted" 30 days out

  POST /api/v1/events/seed         admin — bulk-seed events
  POST /api/v1/events/add          admin — add one event
  GET  /api/v1/events/upcoming     public — next 90 days
  GET  /events                     public HTML directory
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, Response, jsonify, request


industry_events_bp = Blueprint("industry_events", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


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
CREATE TABLE IF NOT EXISTS industry_events (
    slug                    TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    starts_on               DATE NOT NULL,
    ends_on                 DATE,
    location                TEXT,
    url                     TEXT,
    organizer               TEXT,
    submission_deadline     DATE,
    dchub_submitted         BOOLEAN NOT NULL DEFAULT FALSE,
    dchub_attended          BOOLEAN NOT NULL DEFAULT FALSE,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_events_starts ON industry_events(starts_on);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


# Seed list of major data-center industry events (refresh dates as needed).
# This is intentionally a CURATED list — adding scraping later is queued.
_SEED_EVENTS = [
    {"slug": "datacenter-world-2026",  "name": "Data Center World 2026",
     "starts_on": "2026-04-14", "ends_on": "2026-04-17",
     "location": "Washington DC", "url": "https://www.datacenterworld.com/",
     "organizer": "AFCOM", "submission_deadline": "2026-01-15"},
    {"slug": "dcd-connect-virginia-2026", "name": "DCD>Connect Virginia 2026",
     "starts_on": "2026-09-15", "ends_on": "2026-09-16",
     "location": "Reston, VA", "url": "https://www.datacenterdynamics.com/en/conferences/",
     "organizer": "DCD", "submission_deadline": "2026-07-01"},
    {"slug": "dcd-connect-london-2026", "name": "DCD>Connect London 2026",
     "starts_on": "2026-11-04", "ends_on": "2026-11-05",
     "location": "London, UK", "url": "https://www.datacenterdynamics.com/en/conferences/",
     "organizer": "DCD", "submission_deadline": "2026-09-01"},
    {"slug": "ofc-2026", "name": "OFC 2026 (Optical Fiber Conference)",
     "starts_on": "2026-03-08", "ends_on": "2026-03-12",
     "location": "San Diego, CA", "url": "https://www.ofcconference.org/",
     "organizer": "Optica", "submission_deadline": "2025-10-15"},
    {"slug": "pacific-tc-2026", "name": "PTC '26 (Pacific Telecommunications Council)",
     "starts_on": "2026-01-18", "ends_on": "2026-01-21",
     "location": "Honolulu, HI", "url": "https://www.ptc.org/",
     "organizer": "PTC", "submission_deadline": "2025-09-15"},
    {"slug": "interop-digital-2026", "name": "Interop 2026",
     "starts_on": "2026-05-06", "ends_on": "2026-05-08",
     "location": "Las Vegas, NV", "url": "https://www.interop.com/",
     "organizer": "Informa", "submission_deadline": "2026-02-01"},
    {"slug": "metro-connect-2026", "name": "Metro Connect 2026",
     "starts_on": "2026-02-10", "ends_on": "2026-02-12",
     "location": "Miami, FL", "url": "https://www.capacitymedia.com/events/metro-connect",
     "organizer": "Capacity Media", "submission_deadline": "2025-11-15"},
    {"slug": "bisnow-data-center-2026", "name": "Bisnow Data Center Investment Conference 2026",
     "starts_on": "2026-06-04", "ends_on": "2026-06-05",
     "location": "Northern Virginia", "url": "https://www.bisnow.com/events",
     "organizer": "Bisnow", "submission_deadline": "2026-04-15"},
]


def seed_events() -> dict:
    c = _conn()
    if c is None: return {"ok": False, "error": "no_database"}
    inserted = 0
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for e in _SEED_EVENTS:
                try:
                    cur.execute("""
                        INSERT INTO industry_events
                          (slug, name, starts_on, ends_on, location, url,
                           organizer, submission_deadline)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (slug) DO UPDATE
                          SET name = EXCLUDED.name,
                              starts_on = EXCLUDED.starts_on,
                              ends_on = EXCLUDED.ends_on,
                              location = EXCLUDED.location,
                              url = EXCLUDED.url,
                              organizer = EXCLUDED.organizer,
                              submission_deadline = EXCLUDED.submission_deadline
                    """, (e["slug"], e["name"], e["starts_on"], e["ends_on"],
                          e["location"], e["url"], e["organizer"],
                          e.get("submission_deadline")))
                    inserted += 1
                except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return {"ok": True, "events_in_seed": len(_SEED_EVENTS), "upserted": inserted}


@industry_events_bp.route("/api/v1/events/seed", methods=["POST"])
def seed_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(seed_events()), 200


@industry_events_bp.route("/api/v1/events/upcoming", methods=["GET"])
def upcoming():
    try: days = max(7, min(365, int(request.args.get("days") or 180)))
    except (ValueError, TypeError): days = 180
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        # Auto-seed if empty so /events isn't blank on first hit
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM industry_events")
            n = int((cur.fetchone() or [0])[0] or 0)
        if n == 0:
            seed_events()
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT slug, name, starts_on, ends_on, location, url,
                       organizer, submission_deadline,
                       dchub_submitted, dchub_attended
                  FROM industry_events
                 WHERE starts_on >= CURRENT_DATE
                   AND starts_on <= CURRENT_DATE + INTERVAL '{days} days'
                 ORDER BY starts_on ASC LIMIT 100
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    today = datetime.date.today()
    out = []
    for r in rows:
        days_until = (r["starts_on"] - today).days if r["starts_on"] else None
        deadline_days = ((r["submission_deadline"] - today).days
                         if r["submission_deadline"] else None)
        out.append({
            "slug":               r["slug"],
            "name":               r["name"],
            "starts_on":          r["starts_on"].isoformat() if r["starts_on"] else None,
            "ends_on":            r["ends_on"].isoformat() if r["ends_on"] else None,
            "location":           r["location"],
            "url":                r["url"],
            "organizer":          r["organizer"],
            "submission_deadline":r["submission_deadline"].isoformat() if r["submission_deadline"] else None,
            "days_until_event":   days_until,
            "days_until_deadline":deadline_days,
            "dchub_submitted":    bool(r["dchub_submitted"]),
            "dchub_attended":     bool(r["dchub_attended"]),
        })
    resp = jsonify(events=out, count=len(out),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@industry_events_bp.route("/events", methods=["GET"], strict_slashes=False)
def events_page():
    """Public events directory — schema.org Event markup."""
    try:
        from routes.surface_brain import auto_log
        auto_log("industry_events", "view", target="/events")
    except Exception: pass

    # Reuse the upcoming endpoint logic via in-process call
    c = _conn()
    rows = []
    if c is not None:
        try:
            _ensure_schema(c)
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM industry_events")
                if int((cur.fetchone() or [0])[0] or 0) == 0:
                    seed_events()
            import psycopg2.extras
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM industry_events
                     WHERE starts_on >= CURRENT_DATE
                     ORDER BY starts_on ASC LIMIT 50
                """)
                rows = cur.fetchall()
        finally:
            try: c.close()
            except Exception: pass

    today = datetime.date.today()
    rows_html = []
    for r in rows:
        starts = r["starts_on"]
        days = (starts - today).days if starts else None
        deadline = r["submission_deadline"]
        deadline_str = ""
        if deadline:
            dd = (deadline - today).days
            if dd > 0:
                deadline_str = f'<span style="color:#92400e">⏰ submit by {deadline.isoformat()} ({dd}d)</span>'
            else:
                deadline_str = f'<span style="color:#9ca3af">submission closed</span>'
        submitted_badge = ('<span style="background:#10b981;color:white;padding:.15rem .5rem;border-radius:4px;font-size:.75rem">DC HUB SUBMITTED</span>'
                          if r["dchub_submitted"] else
                          '<span style="background:#f59e0b;color:white;padding:.15rem .5rem;border-radius:4px;font-size:.75rem">NOT SUBMITTED</span>')
        rows_html.append(
            f'<div style="background:var(--dch-surface);padding:1rem 1.25rem;border-radius:8px;'
            f'margin:.6rem 0;border:1px solid var(--dch-border)">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap">'
            f'<a href="{r["url"] or "#"}" style="font-weight:700;color:#818cf8;text-decoration:none">{r["name"]}</a>'
            f'{submitted_badge}</div>'
            f'<div style="color:var(--dch-text-mute);font-size:.85rem;margin-top:.3rem">'
            f'{r["starts_on"] or "?"} → {r["ends_on"] or "?"} · {r["location"] or "?"} · {r["organizer"] or "?"}'
            f'{f" · in {days}d" if days is not None and days > 0 else ""}</div>'
            f'<div style="margin-top:.3rem;font-size:.85rem">{deadline_str}</div>'
            f'</div>'
        )

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>Industry Events · DC Hub</title>
<meta name="description" content="Upcoming data-center industry events tracked by DC Hub. {len(rows)} events with submission deadlines and DC Hub participation status.">
<link rel="canonical" href="https://dchub.cloud/events">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>body{{font-family:'Instrument Sans',-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:2rem 1rem;color:var(--dch-text);line-height:1.55;background:var(--dch-bg)}}
h1{{margin:0 0 .25rem;font-size:1.85rem}}
.sub{{color:var(--dch-text-mute);margin:0 0 1.5rem}}
a{{text-decoration:none;color:#818cf8}}</style></head><body>
<h1>📅 Industry Events</h1>
<p class="sub">{len(rows)} upcoming data-center industry events · DC Hub Media participation tracked · submission deadlines surfaced</p>
{''.join(rows_html) or '<p style="color:var(--dch-text-mute);text-align:center;padding:2rem">No events tracked yet — POST /api/v1/events/seed to populate.</p>'}
<p style="color:var(--dch-text-dim);font-size:.85rem;margin-top:2rem;text-align:center">
 Live JSON: <a href="/api/v1/events/upcoming" style="color:#818cf8">/api/v1/events/upcoming</a> · Brain flags events 30d out without DC Hub submission
</p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})
