"""web_ref_visit.py — log ?ref web visits (2026-06-29).

The web/direct experiment tags the free-web CTA links agents relay to humans
as dchub.cloud/playground?ref=mcp-<tool>. This endpoint logs the VISIT (the
intermediate signal between agent-reach and conversion) so the weekly tracker
can show web visits per ref/tool — closing the last attribution gap (we already
see conversions by source; this adds the visit funnel above it).

Lightweight, fire-and-forget, no PII beyond a hashed IP. Public (the playground
is public); same-origin beacon, so no auth.

  GET/POST /api/v1/web/ref?ref=<ref>&p=<page>   → 204, records the visit
"""
from __future__ import annotations
import os, hashlib
from flask import Blueprint, request, jsonify
from routes._swallowed_writes import note_swallowed_write

web_ref_bp = Blueprint("web_ref", __name__)


def _db():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, connect_timeout=4)
        return c
    except Exception:
        return None


@web_ref_bp.route("/api/v1/web/ref", methods=["GET", "POST"])
def web_ref():
    ref = (request.args.get("ref") or "")[:80].strip()
    page = (request.args.get("p") or "")[:80].strip()
    resp = jsonify(ok=bool(ref))
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    if not ref:
        return resp, 204
    c = _db()
    if c is None:
        return resp, 204
    try:
        ip = (request.headers.get("CF-Connecting-IP")
              or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "?")
        iph = hashlib.sha256(ip.encode()).hexdigest()[:16]
        with c, c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS web_ref_visits ("
                " id BIGSERIAL PRIMARY KEY, ref TEXT, page TEXT, ip_hash TEXT,"
                " created_at TIMESTAMPTZ DEFAULT NOW())")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_web_ref_visits_at "
                "ON web_ref_visits (created_at DESC)")
            cur.execute(
                "INSERT INTO web_ref_visits (ref, page, ip_hash) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (ref, page or None, iph))
    except Exception:
        note_swallowed_write("web_ref_visits", where="web_ref_visit.web_ref")
        pass
    finally:
        try: c.close()
        except Exception: pass
    return resp, 204
