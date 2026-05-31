"""
partnership_click_tracker.py — track which /partners#anchor gets clicks.

Phase ZZZZZ-round47.17 (2026-05-25). We now post about /partners
across LinkedIn (daily quad + weekly partnership cycle) + press
releases + direct email. We need to know which anchor gets clicked
so the rotation can favor what's working.

Approach: a /go/partners/<slug> redirect endpoint that:
  1. Logs the click (timestamp, slug, IP-ASN, user-agent, referrer)
  2. 302s to https://dchub.cloud/partners#<anchor>

LinkedIn posts + emails CAN use the bare /partners URL for cleanliness;
for traffic-attribution purposes, the email module + the optional
LinkedIn variant point at /go/partners/<slug> instead.

Endpoints:
  GET  /go/partners/<slug>                           302 + log click
  GET  /api/v1/partnerships/clicks/stats             counts by slug + 7d/30d
  GET  /api/v1/partnerships/clicks/recent            last 50 click rows
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, request, redirect, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

from routes.linkedin_partnership_weekly import _TRACKS as _LINKEDIN_TRACKS

partnership_click_bp = Blueprint("partnership_click", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS partnership_clicks (
                    id          SERIAL PRIMARY KEY,
                    track_slug  TEXT NOT NULL,
                    clicked_at  TIMESTAMPTZ DEFAULT NOW(),
                    ip          TEXT,
                    user_agent  TEXT,
                    referrer    TEXT,
                    source      TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_pc_slug ON partnership_clicks(track_slug, clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_pc_ts ON partnership_clicks(clicked_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


_VALID_SLUGS = {t["slug"] for t in _LINKEDIN_TRACKS}


def _log_click(slug, src=None):
    try:
        with _conn() as c, c.cursor() as cur:
            ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            ua = (request.headers.get("User-Agent", "") or "")[:300]
            ref = (request.headers.get("Referer", "") or "")[:300]
            cur.execute("""
                INSERT INTO partnership_clicks
                  (track_slug, ip, user_agent, referrer, source)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (slug, ip[:80], ua, ref, src or ""))
    except Exception:
        pass


@partnership_click_bp.route("/go/partners", methods=["GET"], strict_slashes=False)
@partnership_click_bp.route("/go/partners/", methods=["GET"], strict_slashes=False)
def go_root():
    _log_click("partners", request.args.get("src"))
    return redirect("https://dchub.cloud/partners", code=302)


@partnership_click_bp.route("/go/partners/<slug>", methods=["GET"])
def go_anchor(slug):
    s = (slug or "").lower().strip()
    if s not in _VALID_SLUGS:
        # Unknown slug → redirect to /partners root, still log so we can
        # see what slugs are being typed wrong
        _log_click(f"unknown:{s[:30]}", request.args.get("src"))
        return redirect("https://dchub.cloud/partners", code=302)

    _log_click(s, request.args.get("src"))
    # Find the actual anchor for this slug
    track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == s), None)
    target = (track or {}).get("url", "https://dchub.cloud/partners")
    return redirect(target, code=302)


@partnership_click_bp.route("/api/v1/partnerships/clicks/stats", methods=["GET"])
def stats():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            # 7d + 30d + all-time per slug
            cur.execute("""
                SELECT track_slug,
                       COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '7 days')  AS d7,
                       COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '30 days') AS d30,
                       COUNT(*) AS total,
                       MAX(clicked_at) AS last_click
                  FROM partnership_clicks
                 GROUP BY track_slug
                 ORDER BY d30 DESC, total DESC
            """)
            by_slug = [{
                "track": r[0],
                "clicks_7d": int(r[1] or 0),
                "clicks_30d": int(r[2] or 0),
                "clicks_total": int(r[3] or 0),
                "last_click": r[4].isoformat() if r[4] else None,
            } for r in cur.fetchall()]

            # By source (linkedin / email / direct)
            cur.execute("""
                SELECT COALESCE(NULLIF(source,''),'direct') AS s, COUNT(*)
                  FROM partnership_clicks
                 WHERE clicked_at > NOW() - INTERVAL '30 days'
                 GROUP BY 1 ORDER BY 2 DESC
            """)
            by_source = [{"source": r[0], "clicks_30d": int(r[1])} for r in cur.fetchall()]

            # Top referrers
            cur.execute("""
                SELECT COALESCE(NULLIF(referrer,''),'(direct)') AS r, COUNT(*)
                  FROM partnership_clicks
                 WHERE clicked_at > NOW() - INTERVAL '30 days'
                 GROUP BY r ORDER BY 2 DESC LIMIT 10
            """)
            top_refs = [{"referrer": r[0][:120], "clicks_30d": int(r[1])} for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM partnership_clicks")
            total_all = cur.fetchone()[0]

        return jsonify({
            "by_track": by_slug,
            "by_source": by_source,
            "top_referrers": top_refs,
            "total_all_time": int(total_all or 0),
            "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }), 200, {"Cache-Control": "public, max-age=300"}
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:160]}"}), 500


@partnership_click_bp.route("/api/v1/partnerships/clicks/recent", methods=["GET"])
def recent():
    if not (_pg and _dsn()):
        return jsonify({"recent": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT track_slug, clicked_at, ip, user_agent, referrer, source
                  FROM partnership_clicks
                 ORDER BY clicked_at DESC LIMIT 50
            """)
            recent = [{
                "track": r[0],
                "clicked_at": r[1].isoformat() if r[1] else None,
                "ip": r[2][:50] if r[2] else None,
                "user_agent": (r[3] or "")[:120],
                "referrer": (r[4] or "")[:120],
                "source": r[5] or "",
            } for r in cur.fetchall()]
        return jsonify({"recent": recent, "count": len(recent)}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140], "recent": []}), 200
